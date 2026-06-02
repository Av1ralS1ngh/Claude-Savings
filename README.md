# Decompute Claude Optimization Gateway

A first platform iteration for **Claude optimization through YSCompress + Yellowstone**.

This project intentionally drops Echelon from the v0 request path. The product is a self-serve gateway that sits in front of Claude and reduces input tokens / repeated context by combining:

- **YSCompress**: context compression (rust core + optional ML) for tool outputs, logs, RAG chunks, files, and long conversation context.
- **Yellowstone**: routing, device/profile awareness, KV-cache estimation, and cost/latency planning.
- **Claude proxy**: Anthropic-compatible `/v1/messages` and OpenAI-compatible `/v1/chat/completions` endpoints.
- **Self-serve platform shell**: API keys, projects, device scan, usage metrics, dashboard, Python SDK, CLI, Docker.

> Important: this does not host Claude weights. It hosts the optimization layer in front of Claude.

## Install the CLI

One line — installs the `decompute` CLI into an isolated venv and links it onto your PATH:

```bash
curl -fsSL https://<host>/install.sh | bash
# or from a checkout:
bash scripts/install.sh
```

Then:

```bash
decompute init        # save gateway URL + API key  ->  ~/.decompute/config.json
decompute doctor      # verify connection, auth, and active capabilities
decompute claude      # route Claude Code through the gateway
decompute vscode .    # configure the current VS Code project to use the gateway
decompute uninstall   # remove ~/.decompute and the CLI shim
```

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

## Compression engine

The gateway bundles the YSCompress **rust core** (SmartCrusher / CodeCompressor) plus an optional **ML** pass (Kompress). Both run in-process — no separate service to start. If the rust core can't be imported, the gateway falls back to a local safe compressor. Run `decompute doctor` (or open the dashboard) to see which engines are active.

## Claude Code through the gateway

Easiest — let the CLI configure it:

```bash
decompute claude                 # writes ~/.decompute/claude.env
source ~/.decompute/claude.env
claude                           # now routed through the gateway
```

Or set the env manually:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=dc_dev_local   # your Decompute key; the gateway holds the real upstream key
```

**VS Code:** `decompute vscode .` writes `.vscode/settings.json` so Claude Code in the integrated terminal routes through the gateway.

For production, run the gateway with `ANTHROPIC_API_KEY` set server-side and `DECOMPUTE_DASHBOARD_AUTH_REQUIRED=true`.

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
POST /v1/messages/count_tokens     Anthropic-compatible token count
GET  /v1/models                    Anthropic-compatible model list
POST /v1/chat/completions          OpenAI-compatible proxy
POST /v1/compression/preview       YSCompress/fallback compression preview
POST /v1/routes/plan               Yellowstone route plan
POST /v1/devices/scan              Local device profile scan
GET  /v1/usage                     Token savings and usage summary
POST /v1/api_keys                  Self-serve API keys
POST /v1/support/bundle            Privacy-safe support bundle (admin/support scope)
```

## Support bundle

When a beta install misbehaves, generate a **privacy-safe** diagnostic zip — never any raw
prompts, CCR payloads, PII-vault mappings, DB dumps, `.env`, or provider keys:

```bash
decompute support bundle          # writes <data-dir>/support/decompute-support-<UTC>.zip
```

It collects allowlisted diagnostics only (runtime, redacted env/config, `doctor` output,
health/capabilities, a metadata-only store snapshot, scrubbed + truncated logs, scrubbed
reports, a manifest, and a privacy report). Every included file is scrubbed: secrets are
hard-redacted (`[REDACTED:*]`), PII is tokenized (`<email_1>`) or redacted, and prompt-like
fields are omitted. A final **leak scan** runs over the archive — `decompute support bundle`
exits non-zero (and `POST /v1/support/bundle` returns 500) if anything slips through, so an
unsafe bundle is never handed back. Admins can also create one over the API:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/support/bundle \
  -H "Content-Type: application/json" -H "x-decompute-api-key: dc_dev_local" \
  -d '{"download": false}'
```

Tune with `DECOMPUTE_SUPPORT_BUNDLE_PII_MODE` (`tokenize`|`redact`|`passthrough`),
`DECOMPUTE_SUPPORT_BUNDLE_INCLUDE_LOGS`, and `DECOMPUTE_SUPPORT_BUNDLE_MAX_LOG_BYTES`.

## Development shape

```text
apps/api/decompute_gateway/
  main.py                  FastAPI routes and Claude proxy
  yscompress.py           YSCompress client + local fallback compressor
  anthropic_provider.py    Claude upstream/mock provider
  yellowstone/             Device profiles, KV estimator, router/planner
  db.py                    Postgres persistence (SQLite for local dev) + Alembic
packages/python-sdk/       Thin SDK + CLI (decompute init/doctor/claude/vscode/uninstall)
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
