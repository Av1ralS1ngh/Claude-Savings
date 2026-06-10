#!/usr/bin/env bash
# Codesign (and optionally notarize) every native binary inside a Python wheel with your Apple
# Developer ID. Use on the engine wheel (Rust _core.so + any Nuitka-compiled .so) before publishing
# to your download server, so macOS doesn't flag the binaries and a hardened-runtime host can load
# them.
#
# It uses `wheel unpack`/`wheel pack` so the dist-info RECORD hashes are REGENERATED after signing —
# signing changes the .so bytes, and a stale RECORD would make pip's integrity check fail.
#
# Usage:
#   DEVELOPER_ID_APP="Developer ID Application: Acme Inc (TEAMID)" \
#     bash scripts/sign_wheel_binaries.sh dist/yscompress-0.22.4-cp312-…-macosx_*.whl
#
#   # also notarize (OPTIONAL — a pip wheel's .so doesn't need it; Gatekeeper doesn't scan
#   # dlopen'd libraries, and a bare .so can't be stapled. Only useful for hardened-runtime hosts
#   # / strict MDM. registers the binaries with Apple):
#   DEVELOPER_ID_APP="Developer ID Application: Jalaj Upadhyay (XRD6UBG79R)" \
#   NOTARIZE=1 NOTARY_PROFILE="decompute-notary" \
#     bash scripts/sign_wheel_binaries.sh dist/yscompress-…-macosx_*.whl
#
# Set up the notary profile ONCE so no password ever lands in a script or env:
#   xcrun notarytool store-credentials decompute-notary \
#     --apple-id <id> --team-id XRD6UBG79R --password <app-specific-password>
#
# Env:
#   DEVELOPER_ID_APP   Developer ID Application identity (REQUIRED to actually sign; else dry-run)
#   NOTARIZE=1         submit the signed binaries to Apple notary (optional; see note above)
#   NOTARY_PROFILE     keychain profile from `xcrun notarytool store-credentials` (RECOMMENDED), or…
#   NOTARY_APPLE_ID + NOTARY_TEAM_ID + NOTARY_PASSWORD   …direct creds (less safe; prefer the profile)
#   OUT                output dir for the re-signed wheel (default: dist/signed)
#   PY                 interpreter (default: python3.12)
set -euo pipefail
cd "$(dirname "$0")/.."

WHEEL="${1:-${WHEEL:-}}"
[ -n "$WHEEL" ] && [ -f "$WHEEL" ] || { echo "usage: sign_wheel_binaries.sh <wheel>  (got: '${WHEEL:-}')" >&2; exit 1; }
DEVELOPER_ID_APP="${DEVELOPER_ID_APP:-}"
NOTARIZE="${NOTARIZE:-0}"
OUT="${OUT:-dist/signed}"
PY="${PY:-$(command -v python3.12 || command -v python3)}"
WORK="build/wheelsign"

case "$WHEEL" in *macosx_*|*macos*) ;; *) echo "  (note: '$WHEEL' isn't a macOS wheel — signing only applies to macOS binaries)";; esac

rm -rf "$WORK"; mkdir -p "$WORK" "$OUT"
VENV="$WORK/venv"; "$PY" -m venv "$VENV"; "$VENV/bin/pip" -q install --upgrade pip wheel >/dev/null

echo "==> unpacking $WHEEL"
"$VENV/bin/python" -m wheel unpack "$WHEEL" -d "$WORK/unpacked" >/dev/null
PKGDIR="$(find "$WORK/unpacked" -maxdepth 1 -mindepth 1 -type d | head -1)"

mapfile -t BINS < <(find "$PKGDIR" -type f \( -name "*.so" -o -name "*.dylib" \))
echo "==> found ${#BINS[@]} native binar$([ ${#BINS[@]} -eq 1 ] && echo y || echo ies)"
for b in "${BINS[@]}"; do echo "   - ${b#$PKGDIR/}"; done

if [ -z "$DEVELOPER_ID_APP" ]; then
  echo "==> DRY RUN (no DEVELOPER_ID_APP set) — would codesign the binaries above"
else
  for b in "${BINS[@]}"; do
    echo "==> codesign ${b#$PKGDIR/}"
    codesign --force --timestamp --options runtime --sign "$DEVELOPER_ID_APP" "$b"
    codesign --verify --strict --verbose=2 "$b"
  done
  if [ "$NOTARIZE" = "1" ]; then
    # Auth: prefer a stored keychain profile (no secret on disk); fall back to direct creds.
    NOTARY_AUTH=()
    if [ -n "${NOTARY_PROFILE:-}" ]; then
      NOTARY_AUTH=(--keychain-profile "$NOTARY_PROFILE")
    elif [ -n "${NOTARY_APPLE_ID:-}" ] && [ -n "${NOTARY_TEAM_ID:-}" ] && [ -n "${NOTARY_PASSWORD:-}" ]; then
      NOTARY_AUTH=(--apple-id "$NOTARY_APPLE_ID" --team-id "$NOTARY_TEAM_ID" --password "$NOTARY_PASSWORD")
      echo "   (using direct creds — prefer 'xcrun notarytool store-credentials' + NOTARY_PROFILE)"
    else
      echo "NOTARIZE=1 needs NOTARY_PROFILE, or NOTARY_APPLE_ID+NOTARY_TEAM_ID+NOTARY_PASSWORD" >&2; exit 1
    fi
    ZIP="$WORK/notarize.zip"
    /usr/bin/ditto -c -k --keepParent "$PKGDIR" "$ZIP"
    echo "==> notarizing ${#BINS[@]} binaries (waits for Apple)"
    xcrun notarytool submit "$ZIP" "${NOTARY_AUTH[@]}" --wait
    echo "   (bare .so/.dylib cannot be stapled; notarization registers them with Apple so"
    echo "    Gatekeeper's online check passes. Stapling applies only to .app/.pkg/.dmg.)"
  fi
fi

echo "==> repacking wheel (regenerates RECORD hashes)"
"$VENV/bin/python" -m wheel pack "$PKGDIR" -d "$OUT" >/dev/null
echo "==> signed wheel:"; ls -1 "$OUT"/*.whl
[ -n "$DEVELOPER_ID_APP" ] || echo "    (UNSIGNED — set DEVELOPER_ID_APP to produce a signed wheel)"
