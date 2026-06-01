#!/bin/bash
# Start the Decompute gateway with the project .env loaded.
# Usage: scripts/run_gateway.sh   (binds 127.0.0.1:8080)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source ./.env
set +a
exec /Users/avii/Documents/YSCompress/.venv-proxy/bin/python3.12 -m uvicorn \
  decompute_gateway.main:app --app-dir apps/api --host 127.0.0.1 --port 8080
