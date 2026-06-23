#!/usr/bin/env bash
# Convenience runner to verify the teacher-student flow with a REAL small local coder model.
#
# This does NOT replace the production paths — it's a local verification helper. It:
#   1. checks you have enough free RAM,
#   2. starts the gateway (SQLite + mock Claude teacher, so no Postgres/API key needed),
#   3. tells you how to start the student model and run a real ts-run.
#
# Default model: Qwen2.5-Coder-3B-Instruct Q4_K_M (~2 GB) — a small coder that FITS 16 GB and gives
# coherent output (4-bit of a good 3B beats 2-bit of a 12B for code). Override with MODEL=... .
#
# Usage:
#   bash scripts/verify_local_student.sh            # prints instructions + starts the gateway
#   MODEL=<repo:quant> WEIGHT_GB=<gb> bash scripts/verify_local_student.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

MODEL="${MODEL:-Qwen/Qwen2.5-Coder-3B-Instruct-GGUF:Q4_K_M}"
WEIGHT_GB="${WEIGHT_GB:-2.0}"
PORT_GW="${PORT_GW:-8088}"
PORT_STUDENT="${PORT_STUDENT:-18080}"
PY="${PY:-$REPO_ROOT/.venv/bin/python}"

# ── RAM sanity check ──────────────────────────────────────────────────────────
FREE_GB=$(vm_stat 2>/dev/null | awk '/Pages free/{f=$3} /Pages inactive/{i=$3} /Pages speculative/{s=$3} END{gsub(/\./,"",f);gsub(/\./,"",i);gsub(/\./,"",s); printf "%.1f",(f+i+s)*4096/1024/1024/1024}')
NEED_GB=$(awk "BEGIN{printf \"%.1f\", ${WEIGHT_GB}*1.6}")
echo "model:        ${MODEL}"
echo "model needs:  ~${NEED_GB} GB RAM to run"
echo "free now:     ~${FREE_GB} GB"
if awk "BEGIN{exit !(${FREE_GB} < ${NEED_GB})}"; then
  echo
  echo "⚠️  Only ~${FREE_GB} GB free but the model needs ~${NEED_GB} GB. Quit other apps (browser tabs,"
  echo "   etc.) and re-run, or it will swap-thrash. Continuing anyway in 5s (Ctrl-C to abort)..."
  sleep 5
fi

cat <<EOF

──────────────────────────────────────────────────────────────────────────────
STEP 1 (this terminal): the gateway is starting on http://127.0.0.1:${PORT_GW}
STEP 2 (a SECOND terminal): start the student model — it auto-downloads on first run:

    MODEL="${MODEL}" WEIGHT_GB=${WEIGHT_GB} PORT=${PORT_STUDENT} \\
      bash scripts/start_student_llama_cpp.sh

STEP 3 (a THIRD terminal): once the student says it's listening, run a real task:

    curl -s http://127.0.0.1:${PORT_GW}/v1/teacher_student/run \\
      -H 'content-type: application/json' \\
      -d '{"task":"Write a Python function is_prime(n).","tests":"assert is_prime(7) and not is_prime(8)"}' \\
      | python3 -m json.tool

    # direct (unverified) call straight to the student:
    curl -s http://127.0.0.1:${PORT_GW}/v1/teacher_student/local-chat \\
      -H 'content-type: application/json' \\
      -d '{"prompt":"Write a Python function is_prime(n)."}' | python3 -m json.tool
──────────────────────────────────────────────────────────────────────────────

EOF

export DECOMPUTE_DATABASE_URL="sqlite+pysqlite:///$HOME/.decompute/verify.db"
export DECOMPUTE_MOCK_ANTHROPIC=true
export DECOMPUTE_LOCAL_STUDENT_ENABLED=true
export DECOMPUTE_LOCAL_STUDENT_BASE_URL="http://127.0.0.1:${PORT_STUDENT}/v1"
export DECOMPUTE_LOCAL_STUDENT_MODEL="${MODEL}"
export DECOMPUTE_PORT="${PORT_GW}"
export PYTHONPATH="$REPO_ROOT/apps/api"

exec "$PY" -m uvicorn decompute_gateway.main:app --host 127.0.0.1 --port "${PORT_GW}"
