.PHONY: setup dev smoke test docker-build docker-run sdk-obfuscate macos-pkg clean

setup:
	python3 -m venv .venv
	. .venv/bin/activate && python -m pip install --upgrade pip
	. .venv/bin/activate && pip install -r apps/api/requirements.txt
	. .venv/bin/activate && pip install -e packages/python-sdk

dev:
	. .venv/bin/activate && python -m uvicorn decompute_gateway.main:app --app-dir apps/api --host 127.0.0.1 --port 8080 --reload

smoke:
	. .venv/bin/activate && python scripts/smoke_test.py

test: smoke

docker-build:
	docker build -t decompute-gateway:local .

docker-run:
	docker run --rm -p 8080:8080 --env-file .env decompute-gateway:local

sdk-obfuscate:
	bash scripts/build_obfuscated_sdk.sh

# Standalone macOS .pkg installer for the `decompute` CLI. Unsigned dry-run by default;
# set OBFUSCATE=1 SIGN=1 + Developer ID / NOTARY_PROFILE for a shippable, notarized installer.
macos-pkg:
	bash scripts/build_macos_pkg.sh

clean:
	rm -rf .venv .decompute build dist *.egg-info packages/python-sdk/build packages/python-sdk/dist
