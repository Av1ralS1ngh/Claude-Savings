#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then
  make setup
fi
cp -n .env.example .env || true
source .venv/bin/activate
python -m uvicorn decompute_gateway.main:app --app-dir apps/api --host 127.0.0.1 --port 8080 --reload
