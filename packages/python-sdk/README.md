# decompute-ai Python SDK + CLI

Thin SDK and CLI for the Decompute Gateway. Core YSCompress compression, Yellowstone routing, usage metering, and the provider proxy stay server-side.

## Install

```bash
# one-line installer (isolated venv + `decompute` on PATH)
curl -fsSL https://<host>/install.sh | bash

# or with pip
pip install decompute-ai
```

## CLI

```bash
decompute init          # save gateway URL + API key  ->  ~/.decompute/config.json
decompute doctor        # diagnose connection, auth, and active capabilities
decompute claude        # write ~/.decompute/claude.env so Claude Code routes through the gateway
decompute vscode .      # write .vscode/settings.json so VS Code's terminal uses the gateway
decompute uninstall     # remove ~/.decompute and the CLI shim

decompute health                          # gateway /health
decompute login --base-url URL --api-key KEY
decompute usage [--from … --to … --format csv]
decompute compress --path FILE            # compression preview
decompute api-key-create --name NAME
```

`decompute claude` / `decompute vscode` set `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` (your `dc_…` key) so Claude Code talks to the gateway; the gateway holds the real upstream key.

## SDK

```python
from decompute_ai import Decompute

client = Decompute(api_key="dc_dev_local", base_url="http://127.0.0.1:8080")
resp = client.chat.completions.create(
    model="claude-sonnet-4-5",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp["usage"])
```
