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

## Gateway obfuscation (only if self-hosted)

If customers run the gateway on their own infra, obfuscate it the same way (needs a license):

```bash
pyarmor gen -O build/gateway -r apps/api/decompute_gateway
# then package build/gateway for your deployment target (Docker image / wheel)
```

**Open decision:** is the gateway *self-hosted by customers* (then obfuscate + ship it,
e.g. a Docker image) or *hosted by us* (then no obfuscation needed — it never leaves our
infra)? This drives whether the gateway build above is part of the release at all.

## Publish to PyPI (final step — deferred)

Before publishing, fill the `TODO(confirm)` fields in `packages/python-sdk/pyproject.toml`
(license, authors, project URLs), then:

```bash
pip install twine
twine upload dist/decompute_ai-*.whl          # all per-platform wheels
```

Use a private index or TestPyPI first to validate the install on a clean machine.

## Build artifacts

`build/`, `dist/`, `*.whl`, and `*.egg-info/` are gitignored — never commit obfuscated
artifacts or wheels; they are reproducible from source via the script above.
