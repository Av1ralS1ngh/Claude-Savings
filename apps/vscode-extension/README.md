**Cut your Claude Code token bill — without changing how you work.**

Decompute runs a lightweight gateway **on your own machine** that compresses the context Claude Code sends to Anthropic. You pay for fewer tokens on every request, with the same model and the same answers.

> 🔒 **Private by design.** Bring your own Anthropic key. Your code and prompts are compressed locally and never touch a Decompute server.

---

## Why Decompute

- ⚡ **Lower cost, same Claude.** A native compression core trims redundant context before each request — often **50–90% fewer tokens** on code, logs, diffs, and structured data.
- 🧩 **Zero workflow change.** Point Claude Code at the local gateway once, then keep using `claude` exactly as you do today.
- 🛡️ **Yours, end to end.** Everything runs locally with your own API key. No prompts, no source, nothing leaves your machine.
- 📊 **See every dollar saved.** A live dashboard shows tokens saved and cost avoided — per request and per workload.

---

## Quick Start

When you install the extension, it detects that the Decompute helper isn't set up yet and offers a **one-time guided setup**:

1. Click **Set Up Decompute** (or **“Decompute: Set up”** in the status bar, or run **`Decompute: Set Up`** from the Command Palette).
2. The wizard walks you through four steps, telling you exactly what to do at each one:
   - **Install the helper** — installs the local Decompute gateway (one command, runs in a terminal).
   - **Connect your Claude key** — paste your `sk-ant-…` key. It's hidden and stays on your machine.
   - **Route Claude Code** — points Claude Code at the local gateway.
   - **Verify** — confirms the gateway is running, then offers a one-click window reload.
3. Reopen a Claude Code panel — it now routes through Decompute. Run **`Decompute: Show Usage and Savings`** to watch the compression live.

That's it. Compression is automatic from then on.

> **Working from a local checkout?** Set `decompute.cliInstallCommand` to your installer (e.g. `bash /path/to/decompute/scripts/install.sh`), or click **Edit Command** in step 1.

---

## Commands

| Command | What it does |
|---|---|
| **Decompute: Set Up** | Installs the helper (if missing), connects your Claude key, routes Claude Code, and verifies — step by step. |
| **Decompute: Show Status** | Gateway, license, and routing status at a glance. |
| **Decompute: Show Usage and Savings** | Opens the savings panel — tokens saved and cost avoided. |
| **Decompute: Enable / Disable Routing** | Turns gateway routing for Claude Code on or off. |
| **Decompute: Open Dashboard** | Opens the live savings dashboard. |
| **Decompute: Activate License** | Activates a Decompute Pro license on this device. |
| **Decompute: Configure Gateway URL** | Points the extension at a self-managed / hosted gateway (advanced). |
| **Decompute: Check Local Gateway Status** | Health-checks a separately installed gateway. |

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `decompute.gatewayUrl` | `http://127.0.0.1:8080` | The gateway the extension talks to. Defaults to the local gateway the setup installs; set a self-managed/hosted URL for advanced setups. |
| `decompute.pollSeconds` | `60` | How often the status bar refreshes license/quota. |
| `decompute.cliInstallCommand` | *(blank)* | Override the installer command the setup runs (e.g. a local checkout). Machine-scoped — a workspace can't set it. |
| `decompute.localGatewayUrl` | *(blank)* | Optional URL for the “Check Local Gateway” command. |

---

## Privacy & Security

- **BYOK.** You use your own Anthropic key. Decompute never asks for or stores model-provider credentials in the extension.
- **Local compression.** Context is compressed by the gateway on your machine before a slimmer request is forwarded to Anthropic.
- **Safe configuration.** Routing is written to `~/.claude/settings.json` only with your consent, atomically, preserving your other settings. The installer runs only from a trusted workspace.
- **Minimal identity.** The extension stores a random device id so the gateway can report license and quota status for this device.

---

## About

Decompute is a cost-control gateway — it optimizes Claude Code traffic, it doesn't host Claude. **Anthropic, Claude, and Claude Code are trademarks of Anthropic.** Decompute is not affiliated with or endorsed by Anthropic.
