# Claude Code setup

## Local gateway mode

Start the gateway:

```bash
make dev
```

Then run Claude Code through it:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=sk-ant-...
claude
```

The gateway implements Anthropic-compatible `/v1/messages` and will forward requests upstream after context compression.

## Recommended dev env

```bash
export DECOMPUTE_MOCK_ANTHROPIC=false
export DECOMPUTE_PROMPT_CACHE=auto
export YSCOMPRESS_BASE_URL=http://127.0.0.1:8787
```

## Bypass compression for debugging

Add a header:

```text
x-yscompress-bypass: true
```

For Claude Code, keep a second terminal with mock mode available when debugging the gateway itself.

## Telemetry

View savings:

```bash
decompute usage
```

Or open:

```text
http://127.0.0.1:8080/dashboard
```
