# AGENTS.md - Decompute Claude Gateway

Read this before making changes in this repo.

## Product Shape

This repo contains the Decompute Claude Gateway and related distribution surfaces. The gateway is a separate FastAPI service that performs compression, routing, quota, license, usage, and savings work. The VS Code extension is only a thin controller/config UI.

Default product flow:

```text
VS Code extension -> Hosted Decompute Gateway -> Claude API
```

Advanced local flow:

```text
VS Code extension -> user-installed Decompute Local Gateway -> Claude API
```

The VSIX must not bundle or implement the gateway/proxy itself.

## VS Code Extension Rules

Keep `apps/vscode-extension` Marketplace-safe:

- Default to a hosted HTTPS gateway such as `https://gateway.decompute.run`.
- Treat local gateways as advanced opt-in configuration only.
- Do not hardcode `http://127.0.0.1:8080` in `package.json`, README, CHANGELOG, `src`, or `dist`.
- Do not collect, ask for, or store an Anthropic API key in the VS Code extension.
- Do not mutate Claude Code settings, shell startup files, or VS Code terminal environment from the extension.
- Do not use Marketplace wording such as intercept, MITM, traffic interception, or proxy Claude Code traffic.
- The extension may show login/license status, usage/savings, gateway URL configuration, dashboard links, local gateway health checks, and a copied setup command.
- The CLI/native installer/Homebrew/Docker path owns local gateway installation and Claude Code configuration.

Before building a VSIX, run tests, rebuild `dist`, package the extension, then inspect package contents and grep the packaged files for banned loopback/API-key/interception terms.

## Repo Guardrails

- Never commit or print secrets from `.env` or local credential files.
- Keep the mock Anthropic development path working.
- Do not change `from headroom import compress` in `apps/api/decompute_gateway/yscompress.py`; that is the runtime compression integration.
- Do not call a YSCompress HTTP endpoint for compression from the gateway.
- Do not revert unrelated user changes in the working tree. Current unrelated streaming-cleanup edits may exist and should be left alone unless explicitly requested.
- If committing, use author `Av1ralS1ngh <aviral_s@mt.iitr.ac.in>`, no co-author trailers, and `--no-verify` only when the user asks for a commit.

## Useful Commands

Gateway tests usually run with the Python 3.12 environment at `~/Documents/YSCompress/.venv-proxy/bin/python3.12`.

VS Code extension commands:

```bash
cd apps/vscode-extension
npm run typecheck
npm test
npm run build
npm run package
```
