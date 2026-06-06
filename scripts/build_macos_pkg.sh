#!/usr/bin/env bash
# Build a (optionally obfuscated, signed, and notarized) macOS .pkg installer for the
# `decompute` CLI. It installs a standalone binary to /usr/local/bin/decompute — users need
# no Python.
#
# Pipeline: [PyArmor obfuscate] -> PyInstaller one-file binary -> [codesign, hardened runtime]
#           -> pkgbuild -> productbuild -> [productsign] -> [notarytool submit + staple].
#
# Credentials are boss-provided via env. With NONE set, it does an UNSIGNED, UN-OBFUSCATED
# dry-run so the build itself can be validated today; add the creds to produce a shippable pkg:
#
#   OBFUSCATE=1                                   # PyArmor (needs a PAID license: `pyarmor reg`)
#   SIGN=1                                        # codesign + productsign + notarize + staple
#   DEVELOPER_ID_APP="Developer ID Application: <Name> (<TEAMID>)"
#   DEVELOPER_ID_INSTALLER="Developer ID Installer: <Name> (<TEAMID>)"
#   NOTARY_PROFILE="decompute-notary"             # from `xcrun notarytool store-credentials`
#   APP_VERSION=0.1.0                             # default: version from the SDK pyproject
#   PY=/path/to/python3.12                        # interpreter to build the venv from
#
# Examples:
#   bash scripts/build_macos_pkg.sh                       # unsigned dry-run (verify the build)
#   OBFUSCATE=1 SIGN=1 DEVELOPER_ID_APP=... DEVELOPER_ID_INSTALLER=... NOTARY_PROFILE=... \
#     bash scripts/build_macos_pkg.sh                     # full release pkg
set -euo pipefail
cd "$(dirname "$0")/.."

OBFUSCATE="${OBFUSCATE:-0}"
SIGN="${SIGN:-0}"
PKG_ID="ai.decompute.cli"
INSTALL_DIR="/usr/local/bin"
PY="${PY:-$(command -v python3.12 || command -v python3)}"
SDK="packages/python-sdk"
SRC="$SDK/src/decompute_ai"
ENTRY="packaging/macos/decompute_entry.py"
ENTITLEMENTS="packaging/macos/entitlements.plist"
APP_VERSION="${APP_VERSION:-$(sed -nE 's/^version *= *"([^"]+)".*/\1/p' "$SDK/pyproject.toml" | head -1)}"
WORK="build/macos/work"
OUT="dist/macos"
rm -rf "$WORK" "$OUT"
mkdir -p "$WORK" "$OUT"

echo "==> decompute ${APP_VERSION}  (obfuscate=${OBFUSCATE} sign=${SIGN})"

# --- 1. Isolated build venv: the SDK + PyInstaller ---------------------------------------
VENV="$WORK/venv"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip pyinstaller >/dev/null
"$VENV/bin/pip" -q install "./$SDK" >/dev/null

# --- 2. Obfuscation (optional; needs a paid PyArmor license) ------------------------------
EXTRA_PYI_ARGS=()
if [ "$OBFUSCATE" = "1" ]; then
  echo "==> PyArmor obfuscating decompute_ai"
  "$VENV/bin/pip" -q install pyarmor >/dev/null
  "$VENV/bin/pyarmor" gen -O "$WORK/obf" "$SRC"
  # Bundle the obfuscated package + its runtime instead of the plain source.
  EXTRA_PYI_ARGS+=(--paths "$WORK/obf" --hidden-import pyarmor_runtime_000000)
fi

# --- 3. PyInstaller one-file binary ------------------------------------------------------
echo "==> PyInstaller bundling"
"$VENV/bin/pyinstaller" --onefile --console --clean --noconfirm \
  --name decompute \
  --distpath "$WORK/dist" --workpath "$WORK/pyi" --specpath "$WORK" \
  --collect-submodules decompute_ai \
  --collect-submodules typer --collect-submodules rich --collect-submodules click \
  "${EXTRA_PYI_ARGS[@]}" \
  "$ENTRY"
BIN="$WORK/dist/decompute"
test -x "$BIN"
echo "==> built $BIN ($("$BIN" --help >/dev/null 2>&1 && echo 'runs --help OK' || echo 'WARN: --help failed'))"

# --- 4. Codesign the binary (hardened runtime) -------------------------------------------
if [ "$SIGN" = "1" ]; then
  : "${DEVELOPER_ID_APP:?set DEVELOPER_ID_APP to your 'Developer ID Application: ...' identity}"
  echo "==> codesign (hardened runtime)"
  codesign --force --timestamp --options runtime \
    --entitlements "$ENTITLEMENTS" --sign "$DEVELOPER_ID_APP" "$BIN"
  codesign --verify --strict --verbose=2 "$BIN"
fi

# --- 5. pkgbuild -> productbuild ---------------------------------------------------------
echo "==> packaging .pkg"
ROOT="$WORK/pkgroot"
mkdir -p "$ROOT$INSTALL_DIR"
install -m 0755 "$BIN" "$ROOT$INSTALL_DIR/decompute"
xattr -cr "$ROOT"   # strip extended attrs so the payload has no ._AppleDouble noise
COMPONENT="$WORK/decompute-component.pkg"
pkgbuild --root "$ROOT" --identifier "$PKG_ID" --version "$APP_VERSION" \
  --install-location / "$COMPONENT"
PKG="$OUT/decompute-${APP_VERSION}.pkg"
productbuild --package "$COMPONENT" "$PKG"

# --- 6. Sign the installer + notarize + staple -------------------------------------------
if [ "$SIGN" = "1" ]; then
  : "${DEVELOPER_ID_INSTALLER:?set DEVELOPER_ID_INSTALLER to your 'Developer ID Installer: ...' identity}"
  : "${NOTARY_PROFILE:?set NOTARY_PROFILE (xcrun notarytool store-credentials <name>)}"
  SIGNED="$OUT/decompute-${APP_VERSION}-signed.pkg"
  productsign --sign "$DEVELOPER_ID_INSTALLER" "$PKG" "$SIGNED"
  mv "$SIGNED" "$PKG"
  echo "==> notarizing (this waits for Apple)"
  xcrun notarytool submit "$PKG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$PKG"
  xcrun stapler validate "$PKG"
fi

echo "==> done: $PKG"
[ "$SIGN" = "1" ] || echo "    (UNSIGNED dry-run — set SIGN=1 + Developer ID + NOTARY_PROFILE for a shippable installer)"
[ "$OBFUSCATE" = "1" ] || echo "    (UN-OBFUSCATED — set OBFUSCATE=1 with a paid PyArmor license to ship obfuscated)"
