#!/usr/bin/env bash
#
# Decompute one-line installer.
#
#   curl -fsSL https://<host>/install.sh | bash
#
# Installs the `decompute` CLI into an isolated venv (~/.decompute/venv), links it
# onto your PATH, and leaves you ready to run `decompute init`. Re-runnable.
#
# Env overrides:
#   DECOMPUTE_HOME    install dir            (default: ~/.decompute)
#   DECOMPUTE_SOURCE  pip spec to install    (default: local repo package, else PyPI "decompute-ai")
#   DECOMPUTE_BIN     dir for the shim       (default: ~/.local/bin)
set -euo pipefail

DECOMPUTE_HOME="${DECOMPUTE_HOME:-$HOME/.decompute}"
VENV="$DECOMPUTE_HOME/venv"
BIN="${DECOMPUTE_BIN:-$HOME/.local/bin}"

# Pick a Python (>=3.11).
PY=""
for cand in python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$(command -v "$cand")"; break; fi
done
[ -n "$PY" ] || { echo "error: python3 (>=3.11) not found on PATH" >&2; exit 1; }

# Resolve the package source: explicit override > local repo checkout > PyPI.
SRC="${DECOMPUTE_SOURCE:-}"
if [ -z "$SRC" ]; then
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
  if [ -n "$here" ] && [ -f "$here/../packages/python-sdk/pyproject.toml" ]; then
    SRC="$(cd "$here/../packages/python-sdk" && pwd)"
  else
    SRC="decompute-ai"   # published package (curl|bash path)
  fi
fi

echo "→ Installing the Decompute CLI"
echo "  python : $PY"
echo "  source : $SRC"
echo "  venv   : $VENV"

"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet "$SRC"

mkdir -p "$BIN"
ln -sf "$VENV/bin/decompute" "$BIN/decompute"

echo "✓ installed: $BIN/decompute"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "  (add to PATH)  export PATH=\"$BIN:\$PATH\"" ;;
esac
echo
echo "Next:"
echo "  decompute init        # set gateway URL + API key"
echo "  decompute doctor      # verify the connection"
echo "  decompute claude      # route Claude Code through the gateway"
