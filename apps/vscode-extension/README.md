# Decompute for VS Code

Route **Claude Code** (and any Anthropic-API tool) in your VS Code terminal through the
**Decompute gateway** — it compresses context and cuts tokens before your request reaches
Claude, on your own key (BYOK). Free tier: **100 compressions/day per device**; over the limit
Claude keeps working, just uncompressed. **Pro** removes the limit.

## Quick start

1. **Get the gateway running** — it does the compression; this extension is its VS Code client:
   ```bash
   pip install decompute-ai     # or the provided wheel
   decompute init               # BYOK setup; starts the local gateway at 127.0.0.1:8080
   ```
2. In VS Code, run **Decompute: Set Claude (Anthropic) Key** (BYOK — stored at `~/.decompute/claude.key`, chmod 600).
3. Run **Decompute: Enable Routing**. The status bar shows your tier (e.g. `Decompute Free 100/100`), and Claude Code now flows through the gateway and compresses.

> **No gateway yet?** The status bar shows *Decompute off / not reachable* and Claude Code keeps working normally (uncompressed) — nothing breaks. Click the status bar → **Get Started**. Routing is **off until you enable it**, and it needs a funded Anthropic key (BYOK), not a claude.ai subscription.

## What it does

- **Routes Claude Code through the gateway — terminal *and* the Claude Code panel.** **OFF by
  default** — run **Decompute: Enable Routing** to turn it on (routing bills your funded Anthropic
  API key, not a claude.ai subscription). When enabled it sets `ANTHROPIC_BASE_URL` + a Decompute
  device header so traffic flows through the gateway:
  - the **Claude Code CLI** in integrated terminals (via VS Code's environment API), and
  - the **Claude Code VS Code extension** (panel) via an `env` block merged into
    `~/.claude/settings.json` — your other settings are preserved, and a settings.json that isn't
    valid JSON is left untouched (never overwritten).

  Remove routing with **Decompute: Disable Routing**. Note: already-running Claude Code sessions and
  open terminals keep routing until you reload the panel / open new terminals.

  Your Claude key is *never* written into VS Code's synced state — it lives only in
  `~/.decompute/claude.key` (chmod 600).
- **Status bar.** Shows `Decompute Pro` or `Decompute 45/100` (compressions left today). Click for actions.
- **Upgrade modal.** When the free quota is spent, prompts: *Upgrade to Pro* / *Activate Key* /
  *Continue without Pro*. Reads the gateway's license contract (`/v1/license/status`).
- **Per-device activation.** Paste a Pro key (`Decompute: Activate Pro License`) or deep-link
  `vscode://decompute.decompute/activate?key=…` from the purchase success page. The license binds
  to this Mac via `~/.decompute/device.id` (shared with the `decompute` CLI).

## Requirements

A running Decompute gateway (default `http://127.0.0.1:8080`). Install the CLI and start it:

```bash
pip install decompute-ai      # or the wheel
decompute init                # BYOK setup
```

Set `decompute.gatewayUrl` if your gateway is elsewhere.

## Commands

| Command | What |
|---|---|
| `Decompute: Enable Routing (compress via gateway)` | turn routing on (terminal + panel) |
| `Decompute: Disable Routing (go direct)` | turn routing off and revert `~/.claude/settings.json` |
| `Decompute: Show Status` | routing state + plan + quota, quick actions |
| `Decompute: Activate Pro License` | paste a license key, bind this device |
| `Decompute: Upgrade to Pro` | open the purchase page |
| `Decompute: Set Claude (Anthropic) Key` | store your BYOK key locally (chmod 600) |
| `Decompute: Re-wire Claude Code Routing` | re-apply routing (terminal + panel) |
| `Decompute: Open Dashboard` | open the gateway dashboard |

## Settings

- `decompute.gatewayUrl` — gateway base URL (default `http://127.0.0.1:8080`)
- `decompute.routeClaudeCodePanel` — route the Claude Code panel + CLI via `~/.claude/settings.json` (default `false`)
- `decompute.autoWireTerminal` — inject routing env into integrated terminals (default `false`)
- `decompute.pollSeconds` — status refresh interval, seconds (default `60`, min `15`)

## Privacy

The extension stores only a random device id (`~/.decompute/device.id`) and, if you use *Set
Claude Key*, your key in `~/.decompute/claude.key` (chmod 600). It never sends your prompts
anywhere except through your own gateway to Anthropic on your key. The gateway stores only
HMAC-hashed device/license identifiers.
