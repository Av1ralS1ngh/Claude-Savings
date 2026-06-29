#!/usr/bin/env bash
#
# Decompute one-line installer.
#
#   curl -fsSL https://pub-1093626231534fd593c5355928f4074d.r2.dev/install.sh | bash
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
#   DECOMPUTE_ENGINE_SPEC    the compression engine   (default: the arm64 cp312/cp313 wheel on the
#                            dist bucket — point at your own wheel for other platforms/Python versions)
#   DECOMPUTE_INDEX_URL      extra pip index (your private server hosting the wheels)
#   DECOMPUTE_LICENSE_PUBLIC_KEYS   trusted verify-key JSON; written to ~/.decompute/gateway.env so
#                            the gateway accepts signed Pro licenses offline (see docs/offline_licensing.md)
set -euo pipefail

DECOMPUTE_HOME="${DECOMPUTE_HOME:-$HOME/.decompute}"
VENV="$DECOMPUTE_HOME/venv"
BIN="${DECOMPUTE_BIN:-$HOME/.local/bin}"

# Pick a Python. The engine ships wheels only for CPython 3.12 and 3.13 (Apple Silicon), so prefer
# those — preferring 3.11 here would pick a version with NO wheel on a Mac that also has 3.12.
PY=""
for cand in python3.12 python3.13 python3; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$(command -v "$cand")"; break; fi
done
# No Python at all: try Homebrew automatically (curl|bash can't prompt), then guide clearly if we still
# can't. Common on a fresh Mac — without this the install just dies with a bare error.
if [ -z "$PY" ] && command -v brew >/dev/null 2>&1; then
  echo "→ Python not found — installing python@3.12 via Homebrew (one-time)…"
  brew install python@3.12 >/dev/null 2>&1 || true
  for cand in python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$(command -v "$cand")"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "error: Python 3.12 or 3.13 is required but wasn't found." >&2
  if command -v brew >/dev/null 2>&1; then
    echo "       Install it:  brew install python@3.12   then re-run this installer." >&2
  else
    echo "       Install Homebrew (https://brew.sh), then 'brew install python@3.12' — or get Python 3.12" >&2
    echo "       from https://www.python.org/downloads/ — then re-run this installer." >&2
  fi
  exit 1
fi

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
sdk_spec="${sdk_spec:-$DIST_URL/decompute_ai-0.2.0-py3-none-any.whl}"
gw_spec="${gw_spec:-$DIST_URL/decompute_gateway-0.2.0-py3-none-any.whl}"
# The engine wheel is Python-version + platform specific, so pick the one matching this venv's
# Python (Apple-Silicon macOS for now; other OS/arch get their own wheels via a pip index later).
PYTAG="$("$PY" -c 'import sys; print(f"cp{sys.version_info.major}{sys.version_info.minor}")' 2>/dev/null || echo unknown)"
engine_spec="${DECOMPUTE_ENGINE_SPEC:-$DIST_URL/yscompress-0.22.4-${PYTAG}-${PYTAG}-macosx_11_0_arm64.whl}"

# Published engine wheels are Apple-Silicon (arm64) + CPython 3.12/3.13 only. When the user hasn't
# pointed DECOMPUTE_ENGINE_SPEC at their own wheel, fail fast with a clear message rather than a cryptic
# pip "no matching distribution"/404 abort on an Intel Mac or an unsupported Python.
if [ -z "${DECOMPUTE_ENGINE_SPEC:-}" ]; then
  arch="$(uname -m)"
  if [ "$arch" != "arm64" ]; then
    echo "error: the Decompute engine currently ships only Apple-Silicon (arm64) wheels; this Mac is $arch." >&2
    echo "       Set DECOMPUTE_ENGINE_SPEC to a compatible wheel to override." >&2
    exit 1
  fi
  case "$PYTAG" in
    cp312|cp313) ;;
    *)
      PYV="$("$PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo '?')"
      echo "error: no Decompute engine wheel for Python $PYTAG (this Mac's python3 is $PYV at $PY)." >&2
      echo "       Supported: Python 3.12 or 3.13 on Apple Silicon. Install Python 3.12 (brew install python@3.12)" >&2
      echo "       and re-run, or set DECOMPUTE_ENGINE_SPEC to a compatible wheel." >&2
      exit 1 ;;
  esac
fi

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
# NOTE: "${arr[@]+"${arr[@]}"}" — the +-guard — is required, not cosmetic. On macOS's /bin/bash
# (3.2.57, what `curl … | bash` runs), expanding an EMPTY array via [@] under `set -u` aborts with
# "PIP_INDEX_ARGS[@]: unbound variable". The guard expands to nothing when empty, the elements when set.
# Pass 1 — resolve + install dependencies. On a fresh venv this pulls everything; on a re-run with
# the same versions it's a no-op and does NOT refresh the wheel CONTENTS.
"$VENV/bin/pip" install --quiet "${PIP_INDEX_ARGS[@]+"${PIP_INDEX_ARGS[@]}"}" "$engine_spec"
"$VENV/bin/pip" install --quiet "${PIP_INDEX_ARGS[@]+"${PIP_INDEX_ARGS[@]}"}" "$gw_spec"
"$VENV/bin/pip" install --quiet "${PIP_INDEX_ARGS[@]+"${PIP_INDEX_ARGS[@]}"}" "$sdk_spec"
# Pass 2 — force the latest wheel CONTENTS even when the version string is unchanged, so re-running
# setup picks up a re-published wheel (a content fix shipped without a version bump). --no-deps keeps
# it fast (deps already resolved in pass 1). This is what makes "publish a fix → every user gets it on
# update" actually work, instead of pip seeing the same version and skipping.
"$VENV/bin/pip" install --quiet --force-reinstall --no-deps "${PIP_INDEX_ARGS[@]+"${PIP_INDEX_ARGS[@]}"}" "$engine_spec"
"$VENV/bin/pip" install --quiet --force-reinstall --no-deps "${PIP_INDEX_ARGS[@]+"${PIP_INDEX_ARGS[@]}"}" "$gw_spec"
"$VENV/bin/pip" install --quiet --force-reinstall --no-deps "${PIP_INDEX_ARGS[@]+"${PIP_INDEX_ARGS[@]}"}" "$sdk_spec"

# Warm the compression model into the HF cache (~150 MB, one-time) so the gateway — which runs OFFLINE
# (for privacy + to avoid a network stall on the model load) — finds it instantly and the very FIRST
# request gets full ML compression instead of the lighter fallback. Online here (the runtime is
# offline); best-effort, so no network just means lighter compression until the model is cached.
echo "→ downloading the compression model (~150 MB, one-time — this is the bulk of the wait)…"
if HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 "$VENV/bin/python" -c \
     "from headroom.transforms.kompress_compressor import _load_kompress; _load_kompress()" >/dev/null 2>&1; then
  echo "  ✓ compression model ready"
else
  echo "  ⚠ couldn't fetch the model now — compression will use the lighter path until it's cached."
  echo "    (re-run this installer when online to enable full ML compression.)"
fi

# Link the commands onto PATH. `decompute claude` discovers the gateway via `decompute-gateway`,
# so it must be linked too (not just `decompute`).
mkdir -p "$BIN"
for cmd in decompute decompute-gateway; do
  if [ -x "$VENV/bin/$cmd" ]; then ln -sf "$VENV/bin/$cmd" "$BIN/$cmd"; fi
done

mkdir -p "$DECOMPUTE_HOME"
GW_ENV="$DECOMPUTE_HOME/gateway.env"

# Packaged installs are BYOK/real-upstream by default. Persist it so a workspace .env with
# DECOMPUTE_MOCK_ANTHROPIC=true cannot make the installed gateway return canned mock SSE.
if [ -f "$GW_ENV" ]; then
  grep -v '^DECOMPUTE_MOCK_ANTHROPIC=' "$GW_ENV" > "$GW_ENV.tmp" 2>/dev/null || true
  mv "$GW_ENV.tmp" "$GW_ENV"
fi
printf 'DECOMPUTE_MOCK_ANTHROPIC=false\n' >> "$GW_ENV"
chmod 600 "$GW_ENV" 2>/dev/null || true

# Pin the data dir to the install dir so the gateway never depends on its launch cwd. launchd starts
# the gateway with cwd=/ (read-only); without an absolute data dir, ensure_dirs() would try to mkdir
# a relative ".decompute" there and the gateway would crash before binding — which looks exactly like
# Claude Code "thinking" forever with no reply. The wheel now defaults to an absolute path too, but
# pin it explicitly so a custom DECOMPUTE_HOME is honored at runtime (launchd doesn't export it).
if [ -f "$GW_ENV" ]; then
  grep -v '^DECOMPUTE_DATA_DIR=' "$GW_ENV" > "$GW_ENV.tmp" 2>/dev/null || true
  mv "$GW_ENV.tmp" "$GW_ENV"
fi
printf 'DECOMPUTE_DATA_DIR=%s\n' "$DECOMPUTE_HOME" >> "$GW_ENV"

# Trust key for offline signed Pro licenses (optional — only if provided at install time).
if [ -n "${DECOMPUTE_LICENSE_PUBLIC_KEYS:-}" ]; then
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

# Put BIN on PATH for FUTURE terminals so `decompute claude` just works — printing a hint isn't enough,
# since ~/.local/bin is not on stock-macOS PATH and the user's shell is zsh, not this curl|bash bash.
# Idempotent (marker-guarded) and bash-3.2-safe. ~/.zprofile is read by zsh login shells; we create it
# if absent (pristine account). We never CREATE ~/.bash_profile (doing so suppresses macOS's default
# /etc/profile→~/.profile sourcing for bash users); we only append if it already exists.
ensure_path_in_rc() {
  rc="$1"
  [ -e "$rc" ] || : > "$rc" || return 0
  if ! grep -q "decompute installer (PATH)" "$rc" 2>/dev/null; then
    printf '\n# added by decompute installer (PATH)\nexport PATH="%s:$PATH"\n' "$BIN" >> "$rc" || true
  fi
}
ensure_path_in_rc "$HOME/.zprofile"
if [ -e "$HOME/.bash_profile" ]; then ensure_path_in_rc "$HOME/.bash_profile"; fi
case ":$PATH:" in
  *":$BIN:"*) echo "  ($BIN already on PATH)" ;;
  *) echo "  Added $BIN to your PATH in ~/.zprofile — open a NEW terminal (or run: source ~/.zprofile) first." ;;
esac

# Keep the gateway alive: a launchd agent that starts it at login and RESTARTS it if it crashes, so
# `decompute claude` always finds it running. macOS only, and purely additive — if this fails, the
# on-demand `decompute claude` start still works. Removed by `Decompute: Uninstall`.
if command -v launchctl >/dev/null 2>&1; then
  AGENTS_DIR="$HOME/Library/LaunchAgents"
  PLIST="$AGENTS_DIR/run.decompute.gateway.plist"
  mkdir -p "$AGENTS_DIR"
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>run.decompute.gateway</string>
  <key>ProgramArguments</key><array><string>$BIN/decompute-gateway</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DECOMPUTE_HOME/gateway.log</string>
  <key>StandardErrorPath</key><string>$DECOMPUTE_HOME/gateway.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
PLIST_EOF
  launchctl unload "$PLIST" >/dev/null 2>&1 || true
  if launchctl load "$PLIST" >/dev/null 2>&1; then
    echo "✓ gateway set to auto-start & self-heal (launchd)"
  fi
fi

# Verify the gateway actually came up. A launchd agent that "loaded" still fails silently if the
# process crashes on startup (bad path, missing dep, port already taken) — better to surface that
# here than let the user discover it later as Claude Code hanging with no reply. Best-effort: poll
# /health and report clearly either way.
GW_PORT="${DECOMPUTE_PORT:-8080}"
printf '  → starting the gateway'
GW_OK=""
for _i in $(seq 1 20); do
  if curl -fsS -m 2 "http://127.0.0.1:${GW_PORT}/health" >/dev/null 2>&1; then GW_OK=1; break; fi
  printf '.'; sleep 1
done
echo
if [ -n "$GW_OK" ]; then
  echo "✓ gateway is up on http://127.0.0.1:${GW_PORT}"
else
  echo "⚠ the gateway didn't respond within 20s — it may still be warming up, or it failed to start."
  echo "    Logs:          tail -n 40 \"$DECOMPUTE_HOME/gateway.log\""
  echo "    Start by hand: \"$BIN/decompute-gateway\""
fi
echo
echo "Next:"
echo "  decompute init        # set gateway URL + paste your Claude (sk-ant-…) key"
echo "  decompute claude      # route Claude Code through the gateway (auto-starts it)"
echo "  decompute doctor      # verify the connection"
echo "  decompute dashboard   # open your savings dashboard ( http://127.0.0.1:${GW_PORT}/dashboard )"

# ── anonymous install beacon (counts only) ────────────────────────────────────
# Counts that an install happened. Sends ONLY: the anonymous dev-... id (so this download joins to
# this device's savings), a coarse os, a CI flag, and the version. NEVER prompts/files/IP/UA/email.
# Honors DO_NOT_TRACK and DECOMPUTE_NO_TELEMETRY. Best-effort: a telemetry failure NEVER breaks the
# install (set +e subshell + curl --fail swallowed). Only SUCCESSFUL installs reach this line, so the
# download count is "successful installs", not "install attempts".
decompute_install_beacon() {
  if [ -n "${DO_NOT_TRACK:-}" ] || [ -n "${DECOMPUTE_NO_TELEMETRY:-}" ]; then
    return 0
  fi

  # Reuse the SAME anonymous device id the gateway/CLI use, minting it the exact same way
  # (dev- + uuid hex, chmod 600) if this is a first-ever install. Send the value TRIMMED of all
  # whitespace/newline so sha256 server-side matches savings.device_hash byte-for-byte.
  id_file="$DECOMPUTE_HOME/device.id"
  install_id=""
  if [ -f "$id_file" ]; then
    install_id="$(tr -d '[:space:]' < "$id_file" 2>/dev/null || true)"
  fi
  if [ -z "$install_id" ]; then
    hex="$("$PY" -c 'import uuid; print(uuid.uuid4().hex)' 2>/dev/null || true)"
    [ -z "$hex" ] && hex="$(uuidgen 2>/dev/null | tr -d - | tr 'A-Z' 'a-z' || true)"
    [ -z "$hex" ] && return 0   # no way to mint an id -> skip silently (fail-open)
    install_id="dev-$hex"
    ( umask 177; printf '%s\n' "$install_id" > "$id_file" ) 2>/dev/null || true
  fi
  # Only beacon a well-formed id (matches the Worker's ^dev-[0-9a-f]{32}$ join contract).
  case "$install_id" in
    dev-*) : ;;
    *) return 0 ;;
  esac

  # Coarse os family only.
  case "$(uname -s 2>/dev/null || echo unknown)" in
    Darwin) bos="darwin" ;;
    Linux)  bos="linux" ;;
    CYGWIN*|MINGW*|MSYS*|Windows_NT) bos="win32" ;;
    *)      bos="unknown" ;;
  esac

  # CI / non-interactive detection. Explicit CI markers win; otherwise treat "neither stdin nor
  # stdout is a tty" as CI/headless (a real person running the one-liner has a tty on stdout even
  # though `curl | bash` pipes stdin). Extra markers catch matrices that don't set bare $CI.
  bci="false"
  if [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ] || [ -n "${GITLAB_CI:-}" ] \
     || [ -n "${JENKINS_URL:-}" ] || [ -n "${BUILDKITE:-}" ] || [ -n "${RUNNER_OS:-}" ]; then
    bci="true"
  elif [ ! -t 0 ] && [ ! -t 1 ]; then
    bci="true"
  fi

  ver="${DECOMPUTE_VERSION:-0.1.0}"
  url="${DECOMPUTE_TELEMETRY_URL:-https://decompute-telemetry.xcomp-decompute.workers.dev}"
  payload="$(printf '{"install_id":"%s","os":"%s","ci":%s,"version":"%s"}' \
              "$install_id" "$bos" "$bci" "$ver")"

  # Fire-and-forget; short timeouts; all output/errors discarded; never fails the install.
  curl -fsS -m 5 -X POST \
    -H 'content-type: application/json' \
    -H 'x-decompute-install: 1' \
    --data "$payload" \
    "$url/v1/event" >/dev/null 2>&1 || true
}
# Run the WHOLE function with set +e in a subshell so no internal line can trip set -e / pipefail.
( set +e; decompute_install_beacon ) >/dev/null 2>&1 || true
