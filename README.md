# Claude Savings

The context compression layer for Claude Code.

> Claude Savings sits on your machine, in front of Claude. It compresses the context Claude Code would otherwise send in full, tool output, logs, files, and the long tail of a session, and then forwards a smaller request. Same model, same kind of answer, fewer tokens.

It does not host Claude. It only runs the layer in front of it. Your Anthropic key stays on your machine, the bulky parts get compressed locally, and the slimmer request is what actually goes to Anthropic.

## What it does

- **Compresses context** before each Claude request. Tool output, logs, code, and a long session history are usually where the tokens go.
- **Keeps the original.** The full text is stored locally, and if Claude needs the exact piece back it can call `ccr_retrieve`.
- **Protects the prompt cache.** On some turns compression is skipped on purpose, because a cache hit saves more than squeezing the prompt again.
- **Speaks Claude's API.** `/v1/messages`, streaming, token counting, and `/v1/models` stay the same shape, so Claude Code and OpenClaw do not need a new client.
- **Shows what you saved.** The dashboard and `decompute usage` record tokens before, tokens sent, tokens saved, and an estimated dollar figure, including the latest request and the median, not only a lifetime average.
- **Stays local.** The VS Code extension is setup, status, and the savings view. The gateway does the work on your machine.

## How it works

```
 Claude Code / OpenClaw / SDK / VS Code
        │   prompts, tool output, logs, files, session history
        ▼
    ┌────────────────────────────────────────────────────┐
    │  Claude Savings gateway   (on your machine)        │
    │  ────────────────────────────────────────────────  │
    │  prompt cache check  →  YSCompress  →  CCR         │
    │                           ├─ SmartCrusher  (JSON)   │
    │                           ├─ CodeCompressor (code)  │
    │                           └─ Kompress      (text)   │
    │                                                    │
    │  Yellowstone plans the route and the cost         │
    │  usage is recorded per request                    │
    └────────────────────────────────────────────────────┘
        │   smaller request, plus ccr_retrieve if Claude needs the original
        ▼
 Claude API
```

A request comes in looking like a normal Claude call.

- **Prompt cache check.** If keeping Claude's cached prefix saves more than compressing it, that turn is left alone and the savings are counted as a cache hit.
- **YSCompress** picks a compressor for the content. SmartCrusher handles JSON and tool results, CodeCompressor handles source, and Kompress handles prose when that model is loaded. Repeated logs and big JSON arrays shrink a lot. Short questions and already dense text barely move.
- **CCR** keeps the original locally. The compressed request carries a reference, and Claude can call `ccr_retrieve` if it needs the exact text back.
- **Yellowstone** looks at the token counts after compression and writes the route and cost plan for that turn. It does not compress the prompt again.
- If the rust core is not installed, the gateway falls back to a simpler local trim so the request still completes. The release benchmark below does not count a run that fell back.

From Claude Code's side, the API shape does not change.

## Proof

These are the floors this repo's release check enforces, in `scripts/rc_benchmark.py` (seed `1729`, benchmark `rc-2026.06.01`). A number only counts if the rust engine ran, CCR was on, and the local fallback did not take over.

| Workload | Floor | What we recorded |
|---|---:|---|
| Code search (100 results) | 90% | reference target 92% |
| GitHub issue triage | 75% | 71.2% → 77.4% |
| Codebase exploration | 50% | reported at the rust-core floor, 53%. ML upside is not claimed |
| SRE incident debugging | 75% | 66% → 75%+ |

```bash
python scripts/rc_benchmark.py
```

Savings follow how repetitive the payload is. The same script also checks that detected emails, phone numbers, and API keys are tokenized or redacted before anything is stored, and that a secret is never passed through.

The live number for your own traffic is on the dashboard, or:

```bash
decompute usage
```

That summary is requests, raw input tokens, tokens actually sent, tokens saved, latest and median savings percent, lifetime savings percent, and estimated dollars saved. It splits the same figures by model, by workload, and by client, so a Claude Code session and an OpenClaw session do not get mixed into one blob.

## Who it works with

| Client | How | Notes |
|---|---|---|
| Claude Code | `decompute claude` | sources `~/.decompute/claude.env`, then you run `claude` as usual |
| VS Code | `decompute vscode .` or the Claude Savings extension | the extension sets things up and shows savings. It does not proxy traffic |
| OpenClaw | point it at the gateway as an Anthropic provider | same `/v1/messages`, token count, and model list |
| Any OpenAI-style client | `POST /v1/chat/completions` | same compression path |

## When it helps, and when it does not

It helps when Claude Code sessions get long and the request is full of tool output, logs, search results, or files you already talked about. That is the traffic the benchmark above is built from.

It does little on a short chat, on prose that is already tight, or on a turn where the prompt cache is the cheaper win. In that last case the gateway skips compression on purpose and says so in the savings view.

## Get started

```bash
curl -fsSL https://<host>/install.sh | bash
# or from a checkout:
bash scripts/install.sh
```

Then:

```bash
decompute init        # save the gateway URL and your key
decompute doctor      # check that the gateway is actually up
decompute claude      # point Claude Code at the gateway
decompute usage       # see the tokens you saved
decompute dashboard   # open the savings dashboard
```

`decompute claude` writes `~/.decompute/claude.env`. Source that file, then run `claude` as usual.

## Run it from this repo

```bash
cp .env.example .env
make setup
make dev
```

Open `http://127.0.0.1:8080/dashboard`.

By default the gateway answers with a mock Claude, so you can work without a real key. To hit real Claude, set this in `.env` and restart `make dev`:

```bash
DECOMPUTE_MOCK_ANTHROPIC=false
ANTHROPIC_API_KEY=sk-ant-...
```

## What is in here

```text
apps/api/decompute_gateway/
  main.py                 routes, the Claude proxy, and usage recording
  yscompress.py           YSCompress call, plus the local fallback
  ccr.py                  original-text store and ccr_retrieve
  yellowstone/            route and cost plan after compression
apps/vscode-extension/    setup, status, and savings in VS Code
packages/python-sdk/      the decompute CLI and a small Python client
scripts/rc_benchmark.py   the release compression gate
```

Claude already speaks these, and the gateway adds a few of its own:

```text
POST /v1/messages              Claude-compatible proxy
POST /v1/messages/count_tokens token count
GET  /v1/models                model list
POST /v1/chat/completions      OpenAI-compatible proxy
POST /v1/compression/preview   see what compression would do
GET  /v1/usage                 tokens saved, by model, workload, and client
GET  /dashboard                live savings
```
