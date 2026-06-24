#!/usr/bin/env bash
# Start a local coding-student model with llama.cpp's `llama-server`, exposing the OpenAI-compatible
# endpoint the gateway expects: http://127.0.0.1:18080/v1.
#
# This is the user-managed local runtime for `decompute ts-run` / `local-chat`. The gateway never
# downloads weights or starts this on its own (plan §"Local runtime is user-controlled by default");
# you run this script deliberately.
#
# DEVICE FEASIBILITY (boss requirement): a 16 GB Mac CANNOT run the 12B coder — Q4_K_M (7.4 GB)
# thrashes swap and freezes the machine; Q2_K loads but produces garbage. This script refuses to
# launch a model whose estimated RAM need exceeds usable RAM, and points you at a smaller model.
#
# Usage:
#   bash scripts/start_student_llama_cpp.sh                       # uses DECOMPUTE_LOCAL_STUDENT_MODEL
#   MODEL=<hf-repo:quant> bash scripts/start_student_llama_cpp.sh
#   PORT=18080 CTX=32768 bash scripts/start_student_llama_cpp.sh
#   FORCE=1 bash scripts/start_student_llama_cpp.sh              # skip the RAM feasibility check
#
# Env:
#   MODEL        llama.cpp -hf model spec (repo:quant). Required (or DECOMPUTE_LOCAL_STUDENT_MODEL).
#   PORT         listen port (default 18080)
#   CTX          context window (default 32768)
#   WEIGHT_GB    model file size in GB, for the feasibility check (default: best-effort 0 = skip)
#   FORCE        set to 1 to bypass the feasibility check (you accept the swap-thrash risk)
set -euo pipefail

PORT="${PORT:-18080}"
CTX="${CTX:-32768}"
MODEL="${MODEL:-${DECOMPUTE_LOCAL_STUDENT_MODEL:-}}"
MODEL_PATH="${MODEL_PATH:-}"          # a local .gguf file -> loaded with -m (no download)
WEIGHT_GB="${WEIGHT_GB:-0}"
FORCE="${FORCE:-0}"

if [[ -z "${MODEL}" && -z "${MODEL_PATH}" ]]; then
  echo "error: set MODEL=<hf-repo:quant> (download) or MODEL_PATH=<file.gguf> (local), or DECOMPUTE_LOCAL_STUDENT_MODEL" >&2
  exit 2
fi
if [[ -n "${MODEL_PATH}" && ! -f "${MODEL_PATH}" ]]; then
  echo "error: MODEL_PATH does not exist: ${MODEL_PATH}" >&2
  exit 2
fi

if ! command -v llama-server >/dev/null 2>&1; then
  echo "error: llama-server not found on PATH. Install llama.cpp first (e.g. 'brew install llama.cpp')." >&2
  exit 3
fi

# ── Device-feasibility check (mirrors yellowstone/student_capability.py) ──────
# usable = total_ram - 6 GB OS reserve; need = weight * 1.6 headroom.
if [[ "${WEIGHT_GB}" != "0" && "${FORCE}" != "1" ]]; then
  if [[ "$(uname)" == "Darwin" ]]; then
    TOTAL_BYTES="$(sysctl -n hw.memsize)"
    TOTAL_GB=$(( TOTAL_BYTES / 1024 / 1024 / 1024 ))
  else
    TOTAL_KB="$(grep MemTotal /proc/meminfo | awk '{print $2}')"
    TOTAL_GB=$(( TOTAL_KB / 1024 / 1024 ))
  fi
  USABLE_GB=$(( TOTAL_GB - 6 ))
  # need = WEIGHT_GB * 1.6, integer math via *16/10.
  NEED_GB=$(awk "BEGIN{printf \"%d\", ${WEIGHT_GB}*1.6 + 0.999}")
  if (( USABLE_GB < NEED_GB )); then
    echo "error: this device has ~${USABLE_GB} GB usable RAM but the model needs ~${NEED_GB} GB." >&2
    echo "       Running it would thrash swap and likely freeze the machine. Use a smaller model/quant" >&2
    echo "       (or route to Claude), or set FORCE=1 to override at your own risk." >&2
    exit 4
  fi
fi

echo "endpoint: http://127.0.0.1:${PORT}/v1   (set DECOMPUTE_LOCAL_STUDENT_BASE_URL to match)"
if [[ -n "${MODEL_PATH}" ]]; then
  # Load the local file directly — no network, uses the model the user already has on disk.
  echo "starting llama-server: file=${MODEL_PATH} port=${PORT} ctx=${CTX}"
  exec llama-server -m "${MODEL_PATH}" --host 127.0.0.1 --port "${PORT}" -c "${CTX}"
else
  # Fetch + cache from Hugging Face on first use.
  echo "starting llama-server: model=${MODEL} port=${PORT} ctx=${CTX}"
  exec llama-server -hf "${MODEL}" --host 127.0.0.1 --port "${PORT}" -c "${CTX}"
fi
