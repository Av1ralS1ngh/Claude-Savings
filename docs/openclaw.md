# Using OpenClaw with the Decompute gateway

[OpenClaw](https://docs.openclaw.ai) is an open-source agent harness (like Claude Code). It speaks the
Anthropic Messages protocol, which is exactly what this gateway proxies — so OpenClaw works through
Decompute with **no gateway code changes**, getting the same YSCompress savings + Yellowstone routing as
Claude Code. You just point OpenClaw at the gateway as a custom Anthropic-compatible provider.

## Why this works

The gateway already implements the full surface an Anthropic harness uses, verified against real
OpenClaw-shaped requests:

| What OpenClaw needs | Gateway endpoint | Status |
|---|---|---|
| Send turns (system + tools, streaming or not) | `POST /v1/messages` | ✅ works |
| Token budgeting | `POST /v1/messages/count_tokens` | ✅ works |
| Model discovery | `GET /v1/models` | ✅ works |
| OpenAI-style fallback | `POST /v1/chat/completions` | ✅ works |
| Streaming (SSE) | `POST /v1/messages` (`stream:true`) | ✅ canonical event sequence |

## Configure OpenClaw

OpenClaw supports custom providers via its `models.providers` config. Add a provider that points at the
gateway with `api: "anthropic-messages"` and the gateway's base URL:

```json5
{
  models: {
    providers: {
      decompute: {
        baseUrl: "http://127.0.0.1:8080",      // your local gateway (or the hosted URL)
        apiKey: "${DECOMPUTE_API_KEY}",         // local dev: dc_dev_local
        api: "anthropic-messages",
        models: [
          { id: "claude-sonnet-4-5", contextWindow: 200000, maxTokens: 8192 },
          { id: "claude-haiku-4-5",  contextWindow: 200000, maxTokens: 8192 },
          { id: "claude-opus-4-8",   contextWindow: 200000, maxTokens: 8192 },
        ],
      },
    },
  },
}
```

Then select a `decompute/...` model in OpenClaw. The gateway compresses each turn and forwards to Claude;
OpenClaw sees normal Anthropic responses.

### Auth

- **Local dev** (gateway with `DECOMPUTE_REQUIRE_AUTH=false`, the default): any key works; use `dc_dev_local`.
- **Hosted / team**: issue a project key and set it as `apiKey`. The gateway accepts it via `x-api-key`
  or `Authorization: Bearer` — both are in the CORS allowlist and the auth path.
- **BYOK passthrough**: if the gateway has no server-side `ANTHROPIC_API_KEY`, it forwards the caller's
  key upstream (when `DECOMPUTE_ALLOW_CLIENT_PROVIDER_KEY=true`).

## Verify it's routing through Decompute

Run an OpenClaw turn, then check the gateway saw it as an `openclaw` client:

```bash
# Quick raw check that the protocol works (what OpenClaw does under the hood):
curl -s http://127.0.0.1:8080/v1/messages \
  -H "x-api-key: dc_dev_local" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -H "user-agent: OpenClaw/0.5" \
  -d '{"model":"claude-haiku-4-5","max_tokens":20,"messages":[{"role":"user","content":"say OK"}]}'
```

The gateway classifies the caller from its `User-Agent` into a `client` tag (`claude_code`, `openclaw`,
`sdk`, `other`) on each usage row — so the dashboard and telemetry can split savings by harness. Only the
bucket is stored, never the raw User-Agent.

```sql
-- savings split by harness (run against the gateway's local DB or the usage views):
SELECT client, COUNT(*) AS requests, SUM(tokens_saved) AS tokens_saved
FROM usage_events GROUP BY client ORDER BY tokens_saved DESC;
```

## Notes

- **Streaming** uses the gateway's existing SSE path (the streaming-disconnect milestone applies to
  OpenClaw too — cancelled streams are tracked).
- **Model names**: OpenClaw declares its own model list (above), so the gateway's `/v1/models` discovery
  is optional — but it works if OpenClaw is configured to discover.
- Nothing here changes Claude Code behavior; both harnesses share the same `/v1/messages` path.
