# Mac setup

## 1. Install basics

```bash
xcode-select --install || true
brew install python@3.11 git
```

## 2. Start the gateway

```bash
cd decompute_claude_gateway
cp .env.example .env
make setup
make dev
```

Open:

```text
http://127.0.0.1:8080/dashboard
http://127.0.0.1:8080/docs
```

## 3. Run smoke test

```bash
source .venv/bin/activate
make smoke
```

## 4. Develop with mock Claude first

Default `.env` uses:

```bash
DECOMPUTE_MOCK_ANTHROPIC=true
```

This lets you build the platform without using paid Claude calls.

## 5. Enable real Claude calls

```bash
DECOMPUTE_MOCK_ANTHROPIC=false
ANTHROPIC_API_KEY=sk-ant-...
```

Restart `make dev`.

## 6. Enable Headroom

```bash
pip install "yscompress[all]"
export YSCOMPRESS_TELEMETRY=off
yscompress proxy --port 8787 --no-telemetry
```

Then set:

```bash
YSCOMPRESS_BASE_URL=http://127.0.0.1:8787
```

Restart the gateway.
