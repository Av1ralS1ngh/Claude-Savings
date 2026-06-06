#!/bin/bash
# Start the Decompute gateway with the project .env loaded.
# Usage: scripts/run_gateway.sh   (binds 127.0.0.1:8080)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source ./.env
set +a
# Load the Kompress ML model deterministically from the local HF cache (no network
# fetch that can silently fall back to rust-only). This makes the kompress_ml
# capability's "will engage" true in the running gateway. Override in .env to allow
# a fresh download.
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
exec /Users/avii/Documents/YSCompress/.venv-proxy/bin/python3.12 -m uvicorn \
  decompute_gateway.main:app --app-dir apps/api --host 127.0.0.1 --port 8080 --reload
