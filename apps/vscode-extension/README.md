# Decompute for VS Code

Route **Claude Code** (and any Anthropic-API tool) in your VS Code terminal through the
**Decompute gateway** — it compresses context and cuts tokens before your request reaches
Claude, on your own key (BYOK). Free tier: **100 compressions/day per device**; over the limit
Claude keeps working, just uncompressed. **Pro** removes the limit.

## What it does

- **Auto-wires routing — terminal *and* the Claude Code panel.** Sets `ANTHROPIC_BASE_URL` +
  Decompute device header so traffic flows through the gateway:
  - the **Claude Code CLI** in integrated terminals (via VS Code's environment API), and
  - the **Claude Code VS Code extension** (panel) via an `env` block merged into
    `~/.claude/settings.json` (your other Claude Code settings are preserved; the routing is
    removed automatically when this extension is disabled, so Claude Code never gets stranded on
    a stopped gateway). Toggle with `decompute.routeClaudeCodePanel`.

  Your Claude key is *never* written into either — it's read from `~/.decompute/claude.key`.
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
| `Decompute: Show Status` | plan + quota, quick actions |
| `Decompute: Activate Pro License` | paste a license key, bind this device |
| `Decompute: Upgrade to Pro` | open the purchase page |
| `Decompute: Set Claude (Anthropic) Key` | store your BYOK key locally (chmod 600) |
| `Decompute: Re-wire Terminal Routing` | re-inject the terminal env |
| `Decompute: Open Dashboard` | open the gateway dashboard |

## Settings

- `decompute.gatewayUrl` — gateway base URL (default `http://127.0.0.1:8080`)
- `decompute.autoWireTerminal` — inject routing env into terminals (default `true`)
- `decompute.pollSeconds` — status refresh interval (default `60`)

## Privacy

The extension stores only a random device id (`~/.decompute/device.id`) and, if you use *Set
Claude Key*, your key in `~/.decompute/claude.key` (chmod 600). It never sends your prompts
anywhere except through your own gateway to Anthropic on your key. The gateway stores only
HMAC-hashed device/license identifiers.
