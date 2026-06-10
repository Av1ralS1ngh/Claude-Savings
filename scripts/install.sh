#!/usr/bin/env bash
#
# Decompute one-line installer.
#
#   curl -fsSL https://decompute.ai/install.sh | bash
#
# Installs the WHOLE local compressor stack into one isolated venv (~/.decompute/venv) and links the
# commands onto your PATH, so you can immediately run `decompute init` then `decompute claude`:
#
#   1. decompute-ai      the `decompute` CLI (init / claude / doctor / vscode)
#   2. decompute-gateway the local gateway that routes Claude Code through compression
#   3. yscompress        the compression engine (the obfuscated + signed wheel from your index)
#
# `decompute claude` starts the gateway by finding `decompute-gateway` on PATH, so BOTH commands are
# linked. Re-runnable (idempotent).
#
# Env overrides (each is a pip install spec — a PyPI name, a local path, or a wheel/URL):
#   DECOMPUTE_HOME            install dir              (default: ~/.decompute)
#   DECOMPUTE_BIN            dir for the shims        (default: ~/.local/bin)
#   DECOMPUTE_SDK_SPEC       the CLI                  (default: local repo, else "decompute-ai")
#   DECOMPUTE_GATEWAY_SPEC   the gateway              (default: local repo, else "decompute-gateway")
#   DECOMPUTE_ENGINE_SPEC    the compression engine   (default: "yscompress" — point at your
#                            obfuscated/signed wheel, e.g. https://dl.decompute.ai/yscompress-…whl)
#   DECOMPUTE_INDEX_URL      extra pip index (your private server hosting the wheels)
#   DECOMPUTE_LICENSE_PUBLIC_KEYS   trusted verify-key JSON; written to ~/.decompute/gateway.env so
#                            the gateway accepts signed Pro licenses offline (see docs/offline_licensing.md)
set -euo pipefail

DECOMPUTE_HOME="${DECOMPUTE_HOME:-$HOME/.decompute}"
VENV="$DECOMPUTE_HOME/venv"
BIN="${DECOMPUTE_BIN:-$HOME/.local/bin}"

# Pick a Python (>=3.11; the engine ships cp311/cp312 wheels).
PY=""
for cand in python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$(command -v "$cand")"; break; fi
done
[ -n "$PY" ] || { echo "error: python3 (>=3.11) not found on PATH" >&2; exit 1; }

# Where the wheels are hosted (your Cloudflare R2 bucket's public URL). Override with DECOMPUTE_DIST_URL.
DIST_URL="${DECOMPUTE_DIST_URL:-https://pub-1093626231534fd593c5355928f4074d.r2.dev}"

# Resolve each package source: explicit env override > local repo checkout > the R2 bucket.
# The CLI + gateway are pure-python "py3-none-any" wheels (one file works on every OS); the engine
# is platform-specific, so it stays a name/override until per-platform wheels are published.
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
sdk_spec="${DECOMPUTE_SDK_SPEC:-}"
gw_spec="${DECOMPUTE_GATEWAY_SPEC:-}"
if [ -z "$sdk_spec" ] && [ -n "$here" ] && [ -f "$here/../packages/python-sdk/pyproject.toml" ]; then
  sdk_spec="$(cd "$here/../packages/python-sdk" && pwd)"
fi
if [ -z "$gw_spec" ] && [ -n "$here" ] && [ -f "$here/../apps/api/pyproject.toml" ]; then
  gw_spec="$(cd "$here/../apps/api" && pwd)"
fi
sdk_spec="${sdk_spec:-$DIST_URL/decompute_ai-0.1.0-py3-none-any.whl}"
gw_spec="${gw_spec:-$DIST_URL/decompute_gateway-0.1.0-py3-none-any.whl}"
# The engine wheel is Python-version + platform specific, so pick the one matching this venv's
# Python (Apple-Silicon macOS for now; other OS/arch get their own wheels via a pip index later).
PYTAG="$("$PY" -c 'import sys; print(f"cp{sys.version_info.major}{sys.version_info.minor}")' 2>/dev/null || echo cp312)"
engine_spec="${DECOMPUTE_ENGINE_SPEC:-$DIST_URL/yscompress-0.22.4-${PYTAG}-${PYTAG}-macosx_11_0_arm64.whl}"

# Extra index (your download server hosting the obfuscated wheels).
PIP_INDEX_ARGS=()
[ -n "${DECOMPUTE_INDEX_URL:-}" ] && PIP_INDEX_ARGS=(--extra-index-url "$DECOMPUTE_INDEX_URL")

echo "→ Installing the Decompute compressor stack"
echo "  python : $PY"
echo "  venv   : $VENV"
echo "  cli    : $sdk_spec"
echo "  gateway: $gw_spec"
echo "  engine : $engine_spec"

"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
# Engine first (the gateway imports it), then gateway, then CLI.
"$VENV/bin/pip" install --quiet "${PIP_INDEX_ARGS[@]}" "$engine_spec"
"$VENV/bin/pip" install --quiet "${PIP_INDEX_ARGS[@]}" "$gw_spec"
"$VENV/bin/pip" install --quiet "${PIP_INDEX_ARGS[@]}" "$sdk_spec"

# Link the commands onto PATH. `decompute claude` discovers the gateway via `decompute-gateway`,
# so it must be linked too (not just `decompute`).
mkdir -p "$BIN"
for cmd in decompute decompute-gateway; do
  if [ -x "$VENV/bin/$cmd" ]; then ln -sf "$VENV/bin/$cmd" "$BIN/$cmd"; fi
done

# Trust key for offline signed Pro licenses (optional — only if provided at install time).
if [ -n "${DECOMPUTE_LICENSE_PUBLIC_KEYS:-}" ]; then
  mkdir -p "$DECOMPUTE_HOME"
  GW_ENV="$DECOMPUTE_HOME/gateway.env"
  # Replace any prior line; keep the rest of gateway.env intact.
  if [ -f "$GW_ENV" ]; then
    grep -v '^DECOMPUTE_LICENSE_PUBLIC_KEYS=' "$GW_ENV" > "$GW_ENV.tmp" 2>/dev/null || true
    mv "$GW_ENV.tmp" "$GW_ENV"
  fi
  printf 'DECOMPUTE_LICENSE_PUBLIC_KEYS=%s\n' "$DECOMPUTE_LICENSE_PUBLIC_KEYS" >> "$GW_ENV"
  chmod 600 "$GW_ENV" 2>/dev/null || true
  echo "✓ wrote license verify-key → $GW_ENV"
fi

echo "✓ installed: $BIN/decompute  +  $BIN/decompute-gateway"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "  (add to PATH)  export PATH=\"$BIN:\$PATH\"" ;;
esac
echo
echo "Next:"
echo "  decompute init        # set gateway URL + paste your Claude (sk-ant-…) key"
echo "  decompute claude      # route Claude Code through the gateway (auto-starts it)"
echo "  decompute doctor      # verify the connection"
