# Decompute Claude Gateway for VS Code

Decompute for VS Code is a thin controller for Decompute Claude Gateway. It lets you connect a Decompute license, view usage and savings, configure the gateway URL, open the dashboard, and copy the Claude Code setup command.

Compression does not run inside this VSIX. It runs in the hosted Decompute Gateway by default, or in a separately installed Decompute Local Gateway for advanced private/local mode.

## Quick Start (first time)

The extension is a controller for the `decompute` CLI/gateway. If you've only installed the extension, it will detect that and set everything up for you:

1. Install the extension. On first launch it offers **Set Up Decompute** (or click **"Decompute: Set up"** in the status bar / run `Decompute: Set Up` from the command palette).
2. The wizard walks you through it, one step at a time, telling you exactly what to do:
   - **Step 1 — Install the helper.** It runs the installer in a terminal and waits until the `decompute` command is available.
   - **Step 2 — Connect your Claude key.** The interactive `decompute init` wizard opens; choose *Anthropic API key*, paste your `sk-ant-…` key (it's hidden and stays on your machine), and accept the defaults.
   - **Step 3 — Route Claude Code.** It runs `decompute configure claude-code` so this editor's Claude Code routes through the gateway.
   - **Step 4 — Verify.** It runs `decompute doctor` and offers a one-click window reload.
3. After the reload, open a Claude Code panel — it now routes through Decompute. Use `Decompute: Show Usage and Savings` or open the dashboard to see compression.

> Local development: set `decompute.cliInstallCommand` to your checkout's installer (e.g. `bash /path/to/decompute/scripts/install.sh`), or click **Edit Command** in step 1.

## Commands

| Command | What it does |
|---|---|
| `Decompute: Set Up` | Installs the `decompute` CLI (if missing), connects your Claude key, routes Claude Code, and verifies — step by step. |
| `Decompute: Get Started` | Opens Decompute setup information. |
| `Decompute: Activate License` | Activates a Decompute license on this device. |
| `Decompute: Upgrade` | Opens the Decompute upgrade page. |
| `Decompute: Show Status` | Shows gateway, license, and quota status. |
| `Decompute: Show Usage and Savings` | Opens the usage and savings panel. |
| `Decompute: Configure Gateway URL` | Sets the Decompute Gateway URL used by the extension. |
| `Decompute: Copy Claude Code Setup Command` | Copies `decompute configure claude-code`. |
| `Decompute: Check Local Gateway Status` | Checks an advanced local gateway URL you configured separately. |
| `Decompute: Open Dashboard` | Opens the Decompute dashboard. |

## Settings

- `decompute.gatewayUrl` - Decompute Gateway URL. The default is `https://gateway.decompute.run`.
- `decompute.localGatewayUrl` - optional advanced local gateway URL for the local status check.
- `decompute.pollSeconds` - status refresh interval in seconds.
- `decompute.cliInstallCommand` - command the setup wizard runs to install the `decompute` CLI when it's missing. Blank uses the default installer; set it to a custom command (e.g. a local checkout's `scripts/install.sh`) for development.

## Privacy

The extension stores a random Decompute device id so the gateway can report license and quota status for this device. It does not ask for or store model-provider credentials. Local gateway installation and Claude Code configuration are handled by the separate Decompute CLI/native installer path.
