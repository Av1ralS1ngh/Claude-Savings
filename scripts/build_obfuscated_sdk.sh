#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null || true
python -m pip install --upgrade build pyarmor
rm -rf build/obfuscated-sdk
mkdir -p build/obfuscated-sdk
pyarmor gen -O build/obfuscated-sdk packages/python-sdk/src/decompute_ai
cp packages/python-sdk/pyproject.toml build/obfuscated-sdk/pyproject.toml
cat > build/obfuscated-sdk/README.md <<'MD'
# Obfuscated decompute-ai SDK

This is a PyArmor-obfuscated thin SDK. It is not a hard security boundary.
Keep core routing/compression/billing logic server-side.
MD
(cd build/obfuscated-sdk && python -m build)
echo "Built obfuscated wheel under build/obfuscated-sdk/dist"
