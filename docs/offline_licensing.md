# Offline signed licensing + engine obfuscation

How Pro/Free is enforced when the compression engine runs **on the user's machine**, and how the
engine is obfuscated, signed, and shipped from your own server.

## Why this exists

The engine is local, so the Pro/Free decision is made locally. The old check
([`licensing.py`](../apps/api/decompute_gateway/licensing.py)) read an "active license" **row from a
local SQLite DB** — fine on your server, but on a user's machine they can just insert a row and get
free Pro. So Pro is now proven by a **cryptographically signed license file** instead.

```
                    YOUR SERVER (secret)                 USER'S MACHINE (ships publicly)
  ┌───────────────────────────────────────┐      ┌────────────────────────────────────────┐
  │ Ed25519 PRIVATE key  (secret manager)  │      │ Ed25519 PUBLIC key  (embedded in build) │
  │ license_admin.py issue ───────────────────────►  ~/.decompute/license.dcmp              │
  │   binds {device_id, tier, expiry}      │ sign │  verify_license() in signed_license.py  │
  └───────────────────────────────────────┘      │   → Pro only if signature + device + not │
                                                  │     expired all check out               │
                                                  └────────────────────────────────────────┘
```

The user can read every shipped `.py`, but **cannot forge a license** without the private key.

## Threat model (what each layer actually buys you)

| Attack | Stopped by | Result |
|---|---|---|
| Forge a Pro license | Ed25519 signature (private key never ships) | **Stopped** |
| Insert a fake DB row | Signed file is now the Pro source, not the DB | **Stopped** |
| Copy one license to N machines | `device_id` binding (`max_devices`) | **Stopped** |
| Use Pro after the subscription lapses | signed `expires_at` (+ offline grace) | **Stopped at expiry** |
| Delete the `verify_license()` call from the code | **Nuitka** compiles it to native `.so` | **Raised, not stopped** |

**Be honest with the boss:** obfuscation is *defense in depth*, not a hard boundary — a determined
attacker with the right tools can still patch native code. The signature is the real protection
(it makes forgery cryptographically impossible); Nuitka just makes patching the check out
expensive. The compression *algorithms* are already maximally protected — they're compiled Rust in
`_core.so`. This whole document is about protecting **revenue**, not the algorithms.

## 1. Generate the signing keypair (once)

```bash
python -m decompute_gateway.license_admin keygen --out-dir ./license-keys
```

- Keep `license-keys/license_private.pem` in a **secret manager**. Never commit it, never ship it.
- The command prints the `DECOMPUTE_LICENSE_PUBLIC_KEYS={"<kid>": "<PEM>"}` line for step 2.

## 2. Configure the engine/gateway with the public verify key

Either bake it into the build (replace `EMBEDDED_LICENSE_PUBLIC_KEYS` in
[`signed_license.py`](../apps/api/decompute_gateway/signed_license.py) before the Nuitka build) or
set the env var on the user's gateway:

```bash
export DECOMPUTE_LICENSE_PUBLIC_KEYS='{"<kid>":"-----BEGIN PUBLIC KEY-----\n…\n-----END PUBLIC KEY-----\n"}'
```

Relevant settings (all in [`config.py`](../apps/api/decompute_gateway/config.py)):
`signed_license_enabled`, `license_file` (default `~/.decompute/license.dcmp`),
`license_public_keys_json`, `license_clock_skew_s`, `license_offline_grace_days`.

With **no** public key configured the signed-license path is inert and the gateway behaves exactly
as before (so your hosted deployment is unaffected).

## 3. Issue a license after a confirmed payment

The buyer's device id is the raw per-machine id from their `~/.decompute/device.id` (what the CLI
sends as `x-decompute-device`); collect it at checkout/activation.

```bash
python -m decompute_gateway.license_admin issue \
  --private-key ./license-keys/license_private.pem \
  --device-fingerprint dev-abc123 \
  --tier pro --days 365 \
  --subject buyer@example.com \
  --out license.dcmp
```

`issue` self-verifies the file before writing it. Use `--days -1` for a perpetual (lifetime) license.

> **Automating this:** call `signed_license.sign_license(...)` from your Stripe webhook /
> activation endpoint with the device id captured at checkout, then return the `.dcmp` text for the
> client to save. The CLI is the manual/ops path.

## 4. User activates

The user drops the file at `~/.decompute/license.dcmp` (or wherever `DECOMPUTE_LICENSE_FILE`
points). The local gateway verifies it on every request and Pro turns on — no network needed.

## 5. Build the obfuscated engine wheel (Nuitka)

```bash
# default: obfuscate the license-critical gateway module(s), validate-only
bash scripts/build_engine_nuitka.sh

# obfuscate engine orchestration and build a wheel:
PKG_DIR=/path/to/YSCompress/headroom PKG_NAME=headroom \
  MODULES="pipeline.py compress.py" MODE=package WHEEL=1 \
  bash scripts/build_engine_nuitka.sh
```

The script compiles the listed `.py` to native `.so`, **removes the source**, proves the compiled
module still imports and verifies a license, then (with `WHEEL=1`) builds the wheel.

- Files with `from . import …` (e.g. `licensing.py`) need `MODE=package` (whole-package compile).
- `signed_license.py` is import-light on purpose so it can be obfuscated standalone — it's the one
  file that **must** be compiled, since it holds the verify logic.

## 6. Sign (and notarize) the wheel's binaries

```bash
DEVELOPER_ID_APP="Developer ID Application: Acme Inc (TEAMID)" \
NOTARIZE=1 NOTARY_PROFILE="decompute-notary" \
  bash scripts/sign_wheel_binaries.sh dist/obfuscated/yourpkg-*.whl
```

Codesigns every `.so`/`.dylib` (Rust core + Nuitka modules) and regenerates the wheel `RECORD`
hashes. A pip wheel's `.so` doesn't strictly need notarization (Gatekeeper doesn't scan
Python-loaded extensions), but signing is good hygiene and the script supports notarization too.

## 7. Publish + the one-line install

Distribution is **wheel-only** (no `.app`/`.pkg`, so no Apple notarization needed — a pip-installed
`.so` isn't Gatekeeper-scanned). [scripts/install.sh](../scripts/install.sh) installs the whole
local stack into one isolated venv (`~/.decompute/venv`) and links both commands onto PATH:

| Package | What | Source |
|---------|------|--------|
| `decompute-ai` | the `decompute` CLI (`init`/`claude`/`doctor`) | `packages/python-sdk` |
| `decompute-gateway` | local gateway; `decompute claude` finds it on PATH and starts it | `apps/api` (now packaged) |
| `yscompress` | the compression engine — ship the **obfuscated + signed** wheel here | your index |

```bash
# user runs one command (the VS Code extension runs this for them):
curl -fsSL https://decompute.ai/install.sh | bash
# then:  decompute init   →   decompute claude
```

To wire everything for production, host the wheels on your index and pass the verify key at install:

```bash
DECOMPUTE_INDEX_URL="https://dl.decompute.ai/simple" \
DECOMPUTE_ENGINE_SPEC="yscompress" \
DECOMPUTE_LICENSE_PUBLIC_KEYS='{"<kid>":"-----BEGIN PUBLIC KEY-----\n…\n-----END PUBLIC KEY-----\n"}' \
  bash scripts/install.sh
```

The installer writes the verify key to `~/.decompute/gateway.env`, which `decompute-gateway` loads
before the app starts — so the gateway accepts signed Pro licenses offline with no further setup.

### Remaining infra (needs your accounts, not code)
1. Publish `decompute-ai`, `decompute-gateway`, and the obfuscated `yscompress` wheel to your index.
2. Serve `scripts/install.sh` at `https://decompute.ai/install.sh` as `text/plain` (today it returns
   the landing-page HTML — see the note in the VS Code extension's setup notes).
3. Bake the license verify key into the engine build (replace `EMBEDDED_LICENSE_PUBLIC_KEYS`) or ship
   it via `DECOMPUTE_LICENSE_PUBLIC_KEYS` as above.

## Test coverage

- [`tests/test_signed_license.py`](../tests/test_signed_license.py) — verifier edge cases
  (tamper, foreign key, wrong device, expiry, grace, skew, malformed, inert-without-key).
- [`tests/test_device_licensing.py`](../tests/test_device_licensing.py) — the resolver grants Pro
  from a signed file and degrades forged/expired/foreign/keyless files to the free tier.
- [`tests/test_license_admin.py`](../tests/test_license_admin.py) — keygen + issue + verify round-trip.
