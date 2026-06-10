"""Issuer CLI: keygen writes a usable keypair; issue mints a device-bound license that the
verifier accepts for that device and rejects for another."""
from __future__ import annotations

import os

from decompute_gateway import signed_license as sl
from decompute_gateway.license_admin import main


def test_keygen_writes_keypair(tmp_path):
    rc = main(["keygen", "--out-dir", str(tmp_path)])
    assert rc == 0
    priv = (tmp_path / "license_private.pem").read_text()
    pub = (tmp_path / "license_public.pem").read_text()
    assert "PRIVATE KEY" in priv and "PUBLIC KEY" in pub
    # private key file is owner-only
    assert (os.stat(tmp_path / "license_private.pem").st_mode & 0o077) == 0


def test_keygen_refuses_overwrite_without_force(tmp_path):
    assert main(["keygen", "--out-dir", str(tmp_path)]) == 0
    assert main(["keygen", "--out-dir", str(tmp_path)]) == 2          # exists -> refuse
    assert main(["keygen", "--out-dir", str(tmp_path), "--force"]) == 0


def test_issue_then_verify_roundtrip(tmp_path, capsys):
    main(["keygen", "--out-dir", str(tmp_path)])
    priv_path = str(tmp_path / "license_private.pem")
    out = str(tmp_path / "license.dcmp")
    rc = main(["issue", "--private-key", priv_path, "--device-fingerprint", "dev-buyer-1",
               "--tier", "pro", "--days", "365", "--out", out])
    assert rc == 0

    pub = (tmp_path / "license_public.pem").read_text()
    kid = sl.key_id(pub)
    text = (tmp_path / "license.dcmp").read_text()
    keys = {kid: pub}
    # Valid for the buyer's machine…
    assert sl.verify_license(text, device_id=sl.canonical_device_id("dev-buyer-1"), public_keys=keys).valid
    # …and useless on any other machine.
    assert not sl.verify_license(text, device_id=sl.canonical_device_id("dev-other"), public_keys=keys).valid


def test_issue_perpetual_when_days_negative(tmp_path):
    main(["keygen", "--out-dir", str(tmp_path)])
    out = str(tmp_path / "perp.dcmp")
    rc = main(["issue", "--private-key", str(tmp_path / "license_private.pem"),
               "--device-fingerprint", "dev-lifetime", "--days", "-1", "--out", out])
    assert rc == 0
    pub = (tmp_path / "license_public.pem").read_text()
    v = sl.verify_license((tmp_path / "perp.dcmp").read_text(),
                          device_id=sl.canonical_device_id("dev-lifetime"), public_keys={sl.key_id(pub): pub})
    assert v.valid and v.expires_at is None


def test_issue_fails_on_missing_private_key(tmp_path):
    assert main(["issue", "--private-key", str(tmp_path / "nope.pem"),
                 "--device-fingerprint", "dev-x", "--out", str(tmp_path / "x.dcmp")]) == 2
