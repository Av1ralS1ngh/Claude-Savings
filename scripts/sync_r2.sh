#!/usr/bin/env bash
# Push the installer + all built wheels to the R2 distribution bucket in one shot, so the files
# users download can never drift from what's in this repo. Run it after ANY rebuild of install.sh
# or the wheels — it overwrites the bucket copies with the current local ones.
#
# One-time setup:
#   npm i -g wrangler        # Cloudflare CLI (or use `npx wrangler` below)
#   wrangler login           # opens a browser; authorizes your Cloudflare account
#
# Usage:
#   bash scripts/sync_r2.sh
#
# Env:
#   DECOMPUTE_R2_BUCKET   bucket name (default: decompute-dist)
set -euo pipefail
cd "$(dirname "$0")/.."

BUCKET="${DECOMPUTE_R2_BUCKET:-decompute-dist}"
WRANGLER="${WRANGLER:-npx wrangler}"

put() {  # $1 = local file, $2 = object key in the bucket
  [ -f "$1" ] || { echo "  ! missing: $1" >&2; return 1; }
  echo "→ $2  ($(du -h "$1" | cut -f1))"
  $WRANGLER r2 object put "$BUCKET/$2" --file "$1" --remote >/dev/null
}

echo "==> syncing to r2://$BUCKET"
put scripts/install.sh install.sh

shopt -s nullglob
wheels=(dist/wheels/*.whl)
[ ${#wheels[@]} -gt 0 ] || { echo "  ! no wheels in dist/wheels — build them first" >&2; exit 1; }
for w in "${wheels[@]}"; do put "$w" "$(basename "$w")"; done

echo "✓ synced install.sh + ${#wheels[@]} wheels"
echo "  verify:  curl -fsSL https://pub-1093626231534fd593c5355928f4074d.r2.dev/install.sh | grep engine_spec="
