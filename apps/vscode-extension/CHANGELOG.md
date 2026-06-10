# Changelog

## 0.1.7

- **First-run setup wizard (`Decompute: Set Up`).** A user who installs only this extension has no `decompute` CLI yet, so the extension now detects that and guides them through the whole thing, telling them exactly what to do at each step:
  1. **Install the helper** — runs the installer in a terminal (overridable via `decompute.cliInstallCommand`) and auto-detects when the `decompute` command appears.
  2. **Connect your Claude key** — opens the interactive `decompute init` wizard with on-screen instructions (choose Anthropic API key, paste your `sk-ant-…` key — it stays on your machine).
  3. **Route Claude Code** — runs `decompute configure claude-code` for the configured gateway.
  4. **Verify** — runs `decompute doctor`, then offers a one-click window reload.
- The status bar now shows **"Decompute: Set up"** (one click into the wizard) until the CLI is installed and configured, instead of a misleading gateway-unreachable state.
- First activation offers the wizard; **"Run Setup"** is also in the status-bar menu and command palette.
- Added the `decompute.cliInstallCommand` setting so teams/local developers can point the installer at a custom command (e.g. `bash /path/to/decompute/scripts/install.sh`).
- Hardened (from an adversarial review): the wizard routes Claude Code **in-process** and verifies over the gateway's `/health` endpoint instead of typing follow-up commands into the still-interactive `init` terminal (which would have been swallowed by its prompts); `decompute.cliInstallCommand` is **machine-scoped** and setup honors Workspace Trust so an untrusted repo can't inject an install command; success is only reported once the gateway is actually reachable; the CLI's `config.toml` is now written atomically; and CLI detection honors the installer's `DECOMPUTE_BIN`/`DECOMPUTE_HOME` overrides.

## 0.1.6

- Rebuilt the VS Code extension as a thin controller/config UI for Decompute Claude Gateway.
- Default gateway is hosted HTTPS: `https://gateway.decompute.run`.
- Added commands for license activation, status, usage/savings, gateway URL configuration, dashboard access, local gateway health check, and copying the Claude Code setup command.
- Removed direct Claude Code configuration mutation and provider credential collection from the VSIX.
- Status bar now reflects actual routing: shows "Decompute off" when Claude Code isn't configured to use the gateway (`~/.claude/settings.json` has no matching `ANTHROPIC_BASE_URL`), instead of showing "Pro"/quota when Decompute isn't in the request path.
- Added **Enable Routing** / **Disable Routing** commands (command palette + the status-bar click menu). They safely write/remove the routing `env` block in `~/.claude/settings.json` — preserving the user's other settings, never overwriting an unparseable file, writing atomically through symlinks with a one-time backup — and offer a one-click window reload so an open Claude Code panel picks up the change. The status bar updates immediately to reflect the new state.
- Kept local gateway mode as an advanced opt-in setting for users who install Decompute Local Gateway separately.

## 0.1.5 and earlier

- Internal pre-release Marketplace packaging iterations.
