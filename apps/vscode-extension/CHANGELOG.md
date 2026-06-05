# Changelog

## 0.1.1
- **Safety fix (incident):** routing is now **OFF by default** and never redirects Claude Code
  until you run **"Decompute: Enable Routing"** and confirm a warning. Routing requires a funded
  Anthropic **API key (BYOK)** — a **claude.ai subscription is not compatible** (routing would
  divert billing to an API key and fail if it has no credits, surfacing as "credit balance too
  low"). The extension also **self-heals** any routing block left by 0.1.0 and reverts cleanly on
  disable/uninstall. Added a one-time welcome and an explicit enable/disable flow.

## 0.1.0
- Initial release: terminal routing, license/quota status bar, free-limit upgrade modal,
  per-device Pro activation (paste or `vscode://` deep link), BYOK key setup, dashboard.
