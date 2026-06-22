#!/usr/bin/env bash
# Gate-1 serve-canary runner: prove the local student SERVES on the live /v1/messages proxy with a
# REAL Claude judge approving — the one transaction the test suite can't exercise (it needs real
# Claude auth, which a mock gateway fails closed on).
#
# This is the decisive internal-canary gate before any external user flips DECOMPUTE_LOCAL_STUDENT_SERVE.
# It starts a gateway with serving + the judge ON, pointed at a real local student, and uses a REAL
# Claude credential for the judge/escalation. Then it sends real proxy requests and shows the enriched
# teacher-student summary so you can watch the judge pass-rate and the tests-backed vs judge-only mix.
#
# Requirements (the whole point — no mocks):
#   * a local student already serving (see scripts/start_student_llama_cpp.sh), default :18080
#   * Docker running IF you also want the tests-backed tier (optional; judge alone serves on the proxy)
#   * a REAL Claude credential for the judge:
#       - ANTHROPIC_API_KEY=sk-ant-...   (BYOK), OR
#       - run real `decompute claude` traffic through this gateway so each request carries its own
#         subscription auth (the judge reuses the request's credential — no key needed).
#
# Usage:
#   ANTHROPIC_API_KEY=sk-ant-... bash scripts/serve_canary.sh          # BYOK judge
#   STUDENT_URL=http://127.0.0.1:18080/v1 bash scripts/serve_canary.sh
#
# Env:
#   ANTHROPIC_API_KEY   real Claude key for the judge (omit only if driving real subscription traffic)
#   STUDENT_URL         local student OpenAI-compatible endpoint (default http://127.0.0.1:18080/v1)
#   STUDENT_MODEL       model id the student reports (default qwen2.5-coder-7b)
#   PORT                gateway port (default 8095, to avoid clashing with a dev gateway on 8080)
#   DB                  sqlite path for canary cases (default /tmp/ts_canary.db)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

STUDENT_URL="${STUDENT_URL:-http://127.0.0.1:18080/v1}"
STUDENT_MODEL="${STUDENT_MODEL:-qwen2.5-coder-7b}"
PORT="${PORT:-8095}"
DB="${DB:-/tmp/ts_canary.db}"
KEY="dc_dev_local"

echo "── Gate-1 serve canary ──────────────────────────────────────────────────────"
echo "student:   $STUDENT_URL ($STUDENT_MODEL)"
echo "gateway:   http://127.0.0.1:$PORT  (SERVE=true, JUDGE_ENABLED=true)"
echo "db:        $DB"

# 1. Preflight: the student must be up (loud, no silent mock).
if ! curl -s -m 5 "${STUDENT_URL%/v1}/health" >/dev/null 2>&1 && ! curl -s -m 5 "$STUDENT_URL/models" >/dev/null 2>&1; then
  echo "error: no local student responding at $STUDENT_URL — start it first (scripts/start_student_llama_cpp.sh)." >&2
  exit 1
fi

# 2. The judge needs real Claude. Either a key here, or real subscription traffic supplies it per-request.
JUDGE_AUTH_NOTE="per-request subscription auth (drive real \`decompute claude\` traffic)"
ANTHROPIC_ENV=()
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  ANTHROPIC_ENV=(ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY")
  JUDGE_AUTH_NOTE="BYOK ANTHROPIC_API_KEY"
fi
echo "judge auth: $JUDGE_AUTH_NOTE"
echo

# 3. Start the gateway with serving + judge on. MOCK_ANTHROPIC is OFF (real Claude judge).
#    Sandbox stays optional: if Docker is present we enable the tests-backed tier too.
SANDBOX_ENV=(DECOMPUTE_TS_SANDBOX_BACKEND=disabled)
if docker info >/dev/null 2>&1; then
  SANDBOX_ENV=(DECOMPUTE_TS_TEST_EXECUTION=true DECOMPUTE_TS_SANDBOX_BACKEND=container DECOMPUTE_TS_SANDBOX_RUNTIME=docker)
  echo "docker present -> tests-backed tier also enabled"
else
  echo "docker absent -> judge-only tier (the proxy serve signal); tests-backed tier off"
fi

echo "starting gateway… (Ctrl-C to stop)"
exec env \
  DECOMPUTE_DATABASE_URL="sqlite+pysqlite:///$DB" \
  DECOMPUTE_ENV=local \
  DECOMPUTE_MOCK_ANTHROPIC=false \
  DECOMPUTE_LOCAL_STUDENT_ENABLED=true \
  DECOMPUTE_LOCAL_STUDENT_BACKEND=llama_cpp \
  DECOMPUTE_LOCAL_STUDENT_BASE_URL="$STUDENT_URL" \
  DECOMPUTE_LOCAL_STUDENT_MODEL="$STUDENT_MODEL" \
  DECOMPUTE_LOCAL_STUDENT_SERVE=true \
  DECOMPUTE_TS_JUDGE_ENABLED=true \
  "${SANDBOX_ENV[@]}" \
  "${ANTHROPIC_ENV[@]}" \
  PYTHONPATH=apps/api:packages/python-sdk/src \
  .venv/bin/python -m uvicorn decompute_gateway.main:app --app-dir apps/api --host 127.0.0.1 --port "$PORT"

# ── After it's up, in another shell, run the decisive test + watch observability ──────────────
#
#   # a real coding request (no tests) -> student writes -> REAL Claude judge reviews -> serves if it passes
#   curl -sN http://127.0.0.1:8095/v1/messages -H "x-decompute-key: dc_dev_local" \
#     -H 'content-type: application/json' \
#     -d '{"model":"claude-opus-4-8","max_tokens":256,"stream":true,
#          "messages":[{"role":"user","content":"Write a Python function to merge two sorted lists."}]}' \
#     -D - | grep -i x-decompute-served-by   # "local-student" == the judge approved + we served
#
#   # watch the canary metrics: accept rate, judge pass-rate, tests-backed vs judge-only
#   curl -s http://127.0.0.1:8095/v1/teacher_student/summary -H "x-decompute-key: dc_dev_local" | python3 -m json.tool
#
# EXIT CRITERIA for this gate (before any external user):
#   * at least N real judge-approved serves observed (served-by: local-student with judge.consulted>0)
#   * judge.pass_rate and accept_evidence.judge_only within agreed bounds
#   * post-hoc spot-check of judge-only serves shows an acceptable false-accept rate
