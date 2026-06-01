# Claude Code setup

## Recommended: configure with the CLI

Start the gateway, then let the CLI wire Claude Code to it:

```bash
scripts/run_gateway.sh        # start the gateway (binds 127.0.0.1:8080)
decompute init                # save gateway URL + key
decompute claude              # writes ~/.decompute/claude.env
source ~/.decompute/claude.env
claude                        # now routed through the gateway
```

`decompute claude --write-profile` appends the `source` line to your shell rc so it's permanent.

## VS Code

```bash
decompute vscode .            # writes .vscode/settings.json (terminal env)
```

Reopen the VS Code integrated terminal; `claude` there now routes through the gateway.

## Manual mode

If you prefer to set the env yourself:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=dc_dev_local   # your Decompute key; the gateway holds the real upstream key
claude
```

The gateway implements Anthropic-compatible `/v1/messages` and forwards upstream after context compression.

## Recommended dev env

```bash
export DECOMPUTE_MOCK_ANTHROPIC=false
export DECOMPUTE_PROMPT_CACHE=auto
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
