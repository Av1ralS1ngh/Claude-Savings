# Decompute Claude Optimization Gateway

A first platform iteration for **Claude optimization through Headroom + Yellowstone**.

This project intentionally drops Echelon from the v0 request path. The product is a self-serve gateway that sits in front of Claude and reduces input tokens / repeated context by combining:

- **Headroom**: context compression for tool outputs, logs, RAG chunks, files, and long conversation context.
- **Yellowstone**: routing, device/profile awareness, KV-cache estimation, and cost/latency planning.
- **Claude proxy**: Anthropic-compatible `/v1/messages` and OpenAI-compatible `/v1/chat/completions` endpoints.
- **Self-serve platform shell**: API keys, projects, device scan, usage metrics, dashboard, Python SDK, CLI, Docker.

> Important: this does not host Claude weights. It hosts the optimization layer in front of Claude.

## Quickstart on Mac

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

In another terminal:

```bash
source .venv/bin/activate
make smoke
```

By default, the gateway uses a mock Claude response so development works without an Anthropic key.

## Use real Claude upstream

Edit `.env`:

```bash
DECOMPUTE_MOCK_ANTHROPIC=false
ANTHROPIC_API_KEY=sk-ant-...
```

Restart `make dev`.

## Add Headroom

```bash
pip install "yscompress[all]"
export YSCOMPRESS_TELEMETRY=off
yscompress proxy --port 8787 --no-telemetry
```

Edit `.env`:

```bash
YSCOMPRESS_BASE_URL=http://127.0.0.1:8787
```

Restart the gateway. If Headroom is not running, the gateway falls back to a local safe compressor.

## Claude Code through the gateway

For local Claude Code proxying:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=sk-ant-...   # sent to gateway and/or used by gateway
```

Then start Claude Code normally. For production, prefer running the gateway with `ANTHROPIC_API_KEY` set server-side and `DECOMPUTE_REQUIRE_AUTH=true` for platform endpoints.

## SDK example

```python
from decompute_ai import Decompute

client = Decompute(api_key="dc_dev_local", base_url="http://127.0.0.1:8080")
client.devices.scan()

huge_log = "ERROR permission denied\n" * 1000
resp = client.chat.completions.create(
    model="claude-sonnet-4-5",
    messages=[
        {"role": "tool", "content": huge_log},
        {"role": "user", "content": "Summarize the failure."},
    ],
)

print(resp["choices"][0]["message"]["content"])
print(resp["usage"])
print(resp["yellowstone"])
```

## Main endpoints

```text
GET  /health
GET  /dashboard
POST /v1/messages                 Anthropic-compatible proxy
POST /v1/chat/completions          OpenAI-compatible proxy
POST /v1/compression/preview       Headroom/fallback compression preview
POST /v1/routes/plan               Yellowstone route plan
POST /v1/devices/scan              Local device profile scan
GET  /v1/usage                     Token savings and usage summary
POST /v1/api_keys                  Self-serve API keys
```

## Development shape

```text
apps/api/decompute_gateway/
  main.py                  FastAPI routes and Claude proxy
  yscompress.py           YSCompress client + local fallback compressor
  anthropic_provider.py    Claude upstream/mock provider
  yellowstone/             Device profiles, KV estimator, router/planner
  db.py                    SQLite persistence
packages/python-sdk/       Thin SDK + CLI
research/yellowstone_legacy Legacy Yellowstone files, not imported in v0
```

## Product direction

v0 is **Claude cost/context optimization**. Later versions can add:

- stronger hosted multi-tenant auth;
- billing and project quotas;
- team dashboards;
- local summarizer/reranker models;
- vLLM/SGLang/llama.cpp local fallback;
- advanced Yellowstone heterogeneous routing;
- Echelon private adaptation as a separate product line.
