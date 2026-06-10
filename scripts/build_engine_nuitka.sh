#!/usr/bin/env bash
# Obfuscate Python modules by compiling them to native .so with Nuitka (Python -> C -> machine code),
# then stage them into the package so the shipped wheel contains NO readable .py for those modules —
# only a native extension, exactly like the Rust `_core.so` the engine already ships.
#
# Why Nuitka and not PyArmor: Nuitka produces real compiled machine code (no bundled "deobfuscate at
# runtime" wrapper to lift), it's free, and it has no license server of its own. The high-value
# compression algorithms are ALREADY protected (compiled Rust core); this protects the Python-layer
# IP and — most importantly — the LICENSE CHECK from being patched out. Obfuscation is defense in
# depth, never a hard boundary: the signature in signed_license.py is what makes Pro unforgeable.
#
# Default target: the license-critical gateway modules. Point it at the engine to obfuscate that too.
#
# Usage:
#   bash scripts/build_engine_nuitka.sh                          # validate-only dry run (no wheel)
#   WHEEL=1 bash scripts/build_engine_nuitka.sh                  # also build the obfuscated wheel
#   PKG_DIR=/path/to/YSCompress/headroom PKG_NAME=headroom \
#     MODULES="pipeline.py compress.py" WHEEL=1 \
#     bash scripts/build_engine_nuitka.sh                        # obfuscate engine orchestration
#
# Env:
#   PKG_DIR    package source dir            (default: apps/api/decompute_gateway)
#   PKG_NAME   importable package name       (default: basename of PKG_DIR)
#   MODULES    space-separated .py paths under PKG_DIR to compile
#                                            (default: "signed_license.py")
#   MODE       modules | package             (default: modules)
#                modules: compile each listed file to its own .so (files with NO relative imports).
#                package: compile the WHOLE package to one .so (use when modules use `from . import`).
#   WHEEL=1    build a pip wheel from the staged (obfuscated) package
#   OUT        output dir                    (default: dist/obfuscated)
#   PY         interpreter                   (default: python3.12)
#
# NOTE on relative imports: a single-file `--module` compile cannot resolve `from . import x`. Such
# modules (e.g. licensing.py -> `from . import signed_license`) must use MODE=package, which compiles
# the package as a unit and preserves intra-package imports. signed_license.py is import-light by
# design precisely so it can be obfuscated standalone — it is the one file that MUST be protected.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

PKG_DIR="${PKG_DIR:-apps/api/decompute_gateway}"
PKG_NAME="${PKG_NAME:-$(basename "$PKG_DIR")}"
MODULES="${MODULES:-signed_license.py}"
MODE="${MODE:-modules}"
WHEEL="${WHEEL:-0}"
OUT="${OUT:-dist/obfuscated}"; case "$OUT" in /*) ;; *) OUT="$REPO/$OUT";; esac
PY="${PY:-$(command -v python3.12 || command -v python3)}"
# Absolute so paths survive the `cd` into the staging dir during verification/wheel build.
WORK="$REPO/build/nuitka"

echo "==> Nuitka obfuscation  (pkg=$PKG_NAME mode=$MODE wheel=$WHEEL)"
[ -d "$PKG_DIR" ] || { echo "PKG_DIR not found: $PKG_DIR" >&2; exit 1; }

rm -rf "$WORK" "$OUT"
mkdir -p "$WORK" "$OUT"

# --- 1. Isolated build venv with Nuitka --------------------------------------------------
VENV="$WORK/venv"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip nuitka >/dev/null
echo "==> nuitka $("$VENV/bin/python" -m nuitka --version 2>/dev/null | head -1)"

# --- 2. Stage a copy of the package, then replace compiled .py with native .so -----------
STAGE="$WORK/stage/$PKG_NAME"
mkdir -p "$(dirname "$STAGE")"
cp -R "$PKG_DIR" "$STAGE"
# Strip __pycache__/.pyc — compiled-bytecode is decompilable and would leak the source we're
# obfuscating, so it must never ride along into the staged (shipped) package.
find "$STAGE" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name "*.pyc" -delete 2>/dev/null || true

# Nuitka flags shared by both modes:
#   --assume-yes-for-downloads  auto-fetch the C cache (ccache) instead of PROMPTING on stdin —
#                               without this, a first-run build appears to "hang" forever.
#   --lto=no                    skip link-time optimization (much faster one-shot C build).
#   --no-progress-bar           clean logs (we capture full output to a file for diagnosis).
NUITKA_FLAGS=(--assume-yes-for-downloads --lto=no --no-progress-bar --output-dir="$WORK/out" --remove-output)

compile_module() {  # $1 = relative path under the package, e.g. signed_license.py
  local rel="$1" src="$STAGE/$1"
  [ -f "$src" ] || { echo "  ! module not found: $rel" >&2; return 1; }
  local base; base="$(basename "$rel" .py)"
  local log="$WORK/nuitka-${base}.log"
  echo "  • compiling $PKG_NAME/$rel -> .so   (C compilation can take ~1-2 min on first run)"
  if ! "$VENV/bin/python" -m nuitka --module "$src" "${NUITKA_FLAGS[@]}" >"$log" 2>&1; then
    echo "  ! Nuitka failed — tail of $log:" >&2; tail -25 "$log" >&2; return 1
  fi
  local so
  so="$(find "$WORK/out" -maxdepth 1 \( -name "${base}.*.so" -o -name "${base}.so" \) 2>/dev/null | head -1)"
  [ -n "$so" ] || { echo "  ! Nuitka produced no .so for $rel" >&2; return 1; }
  rm -f "$src"                      # drop the readable source
  cp "$so" "$STAGE/${base}.so"      # ship the compiled extension instead
}

if [ "$MODE" = "package" ]; then
  echo "==> compiling whole package $PKG_NAME (preserves relative imports; can take several min)"
  PKG_LOG="$WORK/nuitka-${PKG_NAME}.log"
  if ! "$VENV/bin/python" -m nuitka --module "$STAGE" --include-package="$PKG_NAME" \
        "${NUITKA_FLAGS[@]}" >"$PKG_LOG" 2>&1; then
    echo "Nuitka failed — tail of $PKG_LOG:" >&2; tail -25 "$PKG_LOG" >&2; exit 1
  fi
  PKG_SO="$(find "$WORK/out" -maxdepth 1 \( -name "${PKG_NAME}.*.so" -o -name "${PKG_NAME}.so" \) | head -1)"
  [ -n "$PKG_SO" ] || { echo "Nuitka produced no package .so" >&2; exit 1; }
  echo "  • $PKG_SO"
else
  for m in $MODULES; do compile_module "$m"; done
fi

# --- 3. Prove the compiled module imports AND still works (no .py present) ----------------
echo "==> verifying the compiled module loads from .so (source removed)"
"$VENV/bin/pip" -q install cryptography >/dev/null
COMPILED_DIR="$WORK/stage"
( cd "$COMPILED_DIR" && PYTHONPATH="$COMPILED_DIR" "$VENV/bin/python" - "$PKG_NAME" <<'PYEOF'
import sys, importlib, importlib.machinery, pathlib, datetime as dt
pkg = sys.argv[1]
mod = importlib.import_module(f"{pkg}.signed_license")
# Nuitka sets __file__ to the ORIGINAL .py path for compatibility, so checking __file__ is WRONG.
# Prove it's the compiled module two independent ways, and that no readable source ships:
assert getattr(mod, "__compiled__", None) is not None, "module is NOT Nuitka-compiled (no __compiled__ marker)"
assert not isinstance(mod.__loader__, importlib.machinery.SourceFileLoader), "a plain .py was loaded, not the .so"
# The OBFUSCATED module must leave no readable source or decompilable bytecode. (__init__.py stays
# source by design here — only signed_license.py was compiled — and Python writes a transient
# __init__ .pyc on import that isn't part of the staged wheel, so scope this to signed_license.)
leaks = [str(p) for p in pathlib.Path(pkg).rglob("signed_license.py")]
leaks += [str(p) for p in pathlib.Path(pkg).rglob("signed_license*.pyc")]
assert not leaks, f"obfuscated module still leaks source/bytecode: {leaks}"
# It must still FUNCTION: keygen -> sign -> verify round-trip through the compiled code.
priv, pub = mod.generate_keypair()
kid = mod.key_id(pub)
now = dt.datetime.now(dt.timezone.utc)
dev = mod.canonical_device_id("dev-smoke")
lic = mod.sign_license(private_pem=priv, kid=kid, license_id="lic_s", device_id=dev,
                       issued_at=now, expires_at=now + dt.timedelta(days=1))
v = mod.verify_license(lic, device_id=dev, public_keys={kid: pub}, now=now)
assert v.valid, v.reason
print(f"  ✓ {pkg}.signed_license is Nuitka-compiled (loader={type(mod.__loader__).__name__}), "
      f"verified a license, and ships no signed_license .py/.pyc")
PYEOF
) || { echo "verification of compiled module FAILED" >&2; exit 1; }

# --- 4. Optional: build a wheel from the staged (obfuscated) package ----------------------
if [ "$WHEEL" = "1" ]; then
  echo "==> building wheel from staged package"
  if [ -f "$PKG_DIR/../pyproject.toml" ]; then cp "$PKG_DIR/../pyproject.toml" "$WORK/stage/pyproject.toml"; fi
  if [ -f "$PKG_DIR/../setup.py" ]; then cp "$PKG_DIR/../setup.py" "$WORK/stage/setup.py"; fi
  if [ ! -f "$WORK/stage/pyproject.toml" ]; then
    echo "  ! no pyproject.toml beside $PKG_DIR — provide one to build a wheel" >&2
  else
    "$VENV/bin/pip" -q install build >/dev/null
    ( cd "$WORK/stage" && "$VENV/bin/python" -m build --wheel --outdir "$(cd "$OLDPWD" && pwd)/$OUT" >/dev/null )
    echo "==> wheel(s):"; ls -1 "$OUT"/*.whl 2>/dev/null || echo "  (build produced no wheel — check pyproject)"
    echo "    Next: sign the .so inside with scripts/sign_wheel_binaries.sh"
  fi
else
  echo "==> dry run OK (set WHEEL=1 to build the obfuscated wheel)"
fi
echo "==> done"
