from __future__ import annotations

"""Issuer-side admin CLI for offline signed licenses.

Runs on the SIGNING machine / license server ONLY — it touches the PRIVATE key and must never ship
to users. Two commands:

  keygen   Generate an Ed25519 signing keypair (do this ONCE; keep the private PEM in a secret
           manager). Prints the public key as both PEM and the DECOMPUTE_LICENSE_PUBLIC_KEYS JSON
           the gateway/engine is configured with.

  issue    Sign a license file bound to a buyer's device, after a confirmed payment.

Examples:
  python -m decompute_gateway.license_admin keygen --out-dir ./license-keys
  python -m decompute_gateway.license_admin issue \
      --private-key ./license-keys/license_private.pem \
      --device-fingerprint dev-abc123 --tier pro --days 365 --out license.dcmp

The buyer's `--device-fingerprint` is the raw per-machine id from their ~/.decompute/device.id
(the value Claude Code/CLI sends as x-decompute-device). The issued license is then bound to that
machine: it grants Pro only on the device it was minted for.
"""

import argparse
import datetime as _dt
import json
import os
import secrets
import stat
import sys
from typing import Sequence

from . import signed_license as sl


def _write_secret(path: str, text: str) -> None:
    """Write a file as 0600 (private key / license), creating parent dirs."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def cmd_keygen(args: argparse.Namespace) -> int:
    private_pem, public_pem = sl.generate_keypair()
    kid = sl.key_id(public_pem)
    if args.out_dir:
        priv_path = os.path.join(args.out_dir, "license_private.pem")
        pub_path = os.path.join(args.out_dir, "license_public.pem")
        if os.path.exists(priv_path) and not args.force:
            print(f"refusing to overwrite {priv_path} (use --force)", file=sys.stderr)
            return 2
        _write_secret(priv_path, private_pem)
        _write_secret(pub_path, public_pem)
        print(f"✓ wrote {priv_path} (KEEP SECRET, chmod 600) and {pub_path}")
    else:
        print(private_pem)
        print(public_pem)
    print(f"\nkey id (kid): {kid}")
    print("\nConfigure the gateway/engine with this verify key:")
    print("  DECOMPUTE_LICENSE_PUBLIC_KEYS=" + json.dumps({kid: public_pem}))
    print("\nNEVER ship or commit the private key.")
    return 0


def cmd_issue(args: argparse.Namespace) -> int:
    try:
        with open(args.private_key, encoding="utf-8") as f:
            private_pem = f.read()
    except OSError as exc:
        print(f"cannot read --private-key: {exc}", file=sys.stderr)
        return 2

    try:
        kid = sl.key_id(sl.public_pem_from_private(private_pem))
    except ValueError as exc:
        print(f"invalid private key: {exc}", file=sys.stderr)
        return 2

    if args.device_id:
        device_id = args.device_id.strip()
    elif args.device_fingerprint:
        device_id = sl.canonical_device_id(args.device_fingerprint)
    else:
        print("provide --device-fingerprint <raw> or --device-id <canonical>", file=sys.stderr)
        return 2

    now = _dt.datetime.now(_dt.timezone.utc)
    expires_at = None if args.days is None or args.days < 0 else now + _dt.timedelta(days=args.days)
    license_id = args.license_id or ("lic_" + secrets.token_hex(8))
    try:
        text = sl.sign_license(
            private_pem=private_pem, kid=kid, license_id=license_id, device_id=device_id,
            issued_at=now, expires_at=expires_at, tier=args.tier,
            max_devices=args.max_devices, subject=args.subject,
        )
    except ValueError as exc:
        print(f"cannot issue: {exc}", file=sys.stderr)
        return 2

    # Self-check: the file we just minted must verify for this device (catches key/format drift).
    verdict = sl.verify_license(text, device_id=device_id, public_keys={kid: sl.public_pem_from_private(private_pem)}, now=now)
    if not verdict.valid:
        print(f"internal error: issued license failed self-verification ({verdict.reason})", file=sys.stderr)
        return 3

    if args.out:
        _write_secret(args.out, text + "\n")
        print(f"✓ issued {license_id} ({args.tier}) for device {device_id[:12]}… → {args.out}")
        print(f"  expires: {sl._iso(expires_at) if expires_at else 'never (perpetual)'}")
        print("  Deliver this file to the buyer; they place it at ~/.decompute/license.dcmp")
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="decompute-license-admin", description="Issue offline signed Decompute licenses.")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("keygen", help="generate an Ed25519 signing keypair (once)")
    g.add_argument("--out-dir", help="write license_private.pem + license_public.pem here (else print)")
    g.add_argument("--force", action="store_true", help="overwrite an existing private key")
    g.set_defaults(func=cmd_keygen)

    i = sub.add_parser("issue", help="sign a device-bound license file")
    i.add_argument("--private-key", required=True, help="path to license_private.pem")
    i.add_argument("--device-fingerprint", help="buyer's raw machine id (from ~/.decompute/device.id)")
    i.add_argument("--device-id", help="already-canonical device id (sha256 hex), instead of --device-fingerprint")
    i.add_argument("--tier", default="pro", choices=sorted(sl.VALID_TIERS))
    i.add_argument("--days", type=int, default=365, help="validity in days; negative => perpetual")
    i.add_argument("--max-devices", type=int, default=1)
    i.add_argument("--subject", help="optional buyer email / customer id (recorded in the license)")
    i.add_argument("--license-id", help="explicit license id (default: random lic_…)")
    i.add_argument("--out", help="write the license file here (else print to stdout)")
    i.set_defaults(func=cmd_issue)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
