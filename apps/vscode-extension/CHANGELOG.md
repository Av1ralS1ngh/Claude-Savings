# Changelog

## 0.1.14

- **Much higher compression on real work.** Attached files, pasted logs, and HTML now compress up to ~99% (previously ~0%). The engine was skipping structured content blocks and sending HTML to the prose compressor (which can't shrink markup); it now routes HTML/log content through the right compressor and chains the ML compressor on top, and large attached/read files are no longer left verbatim. Code you ask it to *review/analyze* stays protected, and your short question is never altered.
- **Updates actually propagate.** Setup now force-refreshes the local engine/gateway, so a re-publish reaches every machine on the next setup. Existing users get a one-time "update your engine" prompt.

## 0.1.13

- **Pro renewals keep working.** The extension auto-refreshes your signed license on startup, so subscription renewals propagate and an offline Pro license doesn't lapse while you keep paying.
- **Cleaner uninstall.** `Decompute: Uninstall` now also removes the gateway's launchd auto-start agent (so nothing keeps the gateway running after removal).

## 0.1.12

- **One-stop uninstall.** `Decompute: Uninstall` now also uninstalls the extension itself (and disposes the status bar immediately), so you don't have to remove it separately from the Extensions view — reload to finish. Fixes "Decompute Set up" lingering in the status bar after removal.

## 0.1.11

- **Requires the Claude Code extension first.** Setup now checks for the Claude Code extension and offers to install it before configuring Decompute, so a new user is guided in the right order (Decompute is a controller that sits in front of Claude Code).
- **Clean uninstall.** New `Decompute: Uninstall` command (also in the status menu) stops the gateway and removes the CLI, gateway, config, PATH entry, and Decompute's Claude Code routing — then clears onboarding state so a later reinstall prompts setup again from scratch. (VS Code's marketplace "Uninstall" only removes the extension itself, not the installed helper.)

## 0.1.10

- **Buy Pro from the status menu.** `Upgrade to Pro` opens Stripe checkout tagged with this machine's device id, so the license issued after payment binds to exactly this device.
- **One-click activation.** New `Activate Pro` action (and `Decompute: Activate Pro` command) fetches your signed Pro license after payment and installs it — the gateway then verifies it offline. No copy-pasting keys.
- New settings `decompute.checkoutUrl` and `decompute.licenseApiUrl` to point at your Stripe checkout link and license service.

## 0.1.9

- **Readable status menu.** `Decompute: Show Status` now opens a proper, scrollable menu with full labels, icons, and descriptions — instead of a row of message buttons VS Code truncated to "En…", "Usa…", "Cop…". The gateway URL, routing state, and plan show in the menu title.

## 0.1.8

- **Local-first by default — and it actually works out of the box.** The extension now defaults to the gateway the setup installs on your machine (`http://127.0.0.1:8080`) instead of a hosted URL, so first-run setup completes end to end. Compression runs locally with your own key (BYOK).
- **Rewritten, accurate listing.** New benefit-focused overview, an up-to-date Quick Start that matches the guided setup wizard, and a clearer commands/settings reference.
- Polished onboarding copy and suppressed the non-HTTPS warning for the local gateway (it's loopback by design).

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
