# Release build — obfuscation & packaging

## Product shape

The valuable IP — the **YSCompress engine** — is a compiled **Rust** core and runs
**server-side** on the gateway. The thing users install (`decompute-ai`) is a **thin HTTP
client/CLI**. So the obfuscation strategy is:

- **Client (`decompute-ai`)** → ship a PyArmor-obfuscated wheel to PyPI (no readable source).
- **Gateway (`decompute_gateway`)** → the Rust core is already compiled; the Python
  orchestration is obfuscated *only if the gateway is self-hosted by customers* (see below).

## Build the obfuscated CLI wheel

```bash
pip install pyarmor                      # production needs a PyArmor LICENSE (see limits below)
python scripts/build_release.py --verify
# -> dist/decompute_ai-<ver>-<py>-<platform>.whl   (installed + smoke-tested in a temp venv)
```

`scripts/build_release.py` obfuscates `decompute_ai`, assembles it with the PyArmor runtime,
reuses the `[project]` metadata from `packages/python-sdk/pyproject.toml`, and builds the wheel.
`--verify` installs the wheel into a throwaway venv and runs `decompute --help` from it.

**Platform tags.** The wheel bundles PyArmor's native runtime `.so`, so it is **not**
pure-Python — the build forces a platform tag (e.g. `cp312-cp312-macosx_..._arm64`). Build
once **per target platform + Python version** (macOS arm64/x86, Linux, Windows) and upload
all of them; pip then picks the right one. (A `py3-none-any` tag here would be a bug — it
would let a Linux user install the macOS `.so`.)

## PyArmor trial vs license

The **trial** obfuscates only small scripts and caps total work — fine for the CLI (small
files), but it errors `out of license` on the gateway's larger files (`main.py` ~57KB,
`support_bundle.py`/`db.py` ~38KB). **A paid PyArmor license is required** for:
- production CLI builds (remove the trial watermark/limits), and
- obfuscating the gateway at all.

## Gateway obfuscation — NOT needed (hosted by us)

**Decision:** the gateway is a **hosted website** run by us (free/pro/enterprise tiers), so its
Python never ships to customers and needs no obfuscation. Only the thin `decompute-ai` client
goes out (PyPI), and that is obfuscated above.

If an enterprise tier later offers a *self-hosted* gateway, obfuscate it the same way (a license
is required — the trial errors `out of license` on the gateway's larger files):

```bash
pyarmor gen -O build/gateway -r apps/api/decompute_gateway
# then ship the obfuscated tree as a Docker image
```

## CI: multi-platform release

`.github/workflows/release.yml` builds the obfuscated wheel for the full matrix
(ubuntu / macos-arm64 / macos-x86 / windows × Python 3.10–3.13) and publishes on a `v*` tag.

Required repository secrets:
- `PYARMOR_REGFILE_B64` — base64 of your PyArmor license regfile (`base64 -i pyarmor-regfile.zip`).
- `PYPI_API_TOKEN` — PyPI API token (used only by the publish job).

Cut a release: `git tag v0.1.0 && git push origin v0.1.0`. The publish job runs `twine check`
then `twine upload`; it's gated behind a GitHub *environment* (`pypi`) — add a manual-approval
rule there, and validate on TestPyPI first.

## Publish manually (if not using CI)

```bash
pip install twine
python -m twine check dist/*.whl
python -m twine upload dist/*.whl          # all per-platform wheels
```

The package metadata (license/authors/URLs) in `packages/python-sdk/pyproject.toml` is finalized;
adjust the author email / homepage there if the company prefers different values.

## Build artifacts

`build/`, `dist/`, `*.whl`, and `*.egg-info/` are gitignored — never commit obfuscated
artifacts or wheels; they are reproducible from source via the script above.
