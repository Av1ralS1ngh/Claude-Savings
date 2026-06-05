# Changelog

## 0.1.2
- **Data-safety:** `~/.claude/settings.json` is never overwritten when it isn't valid JSON
  (comments/corrupt) — it's left untouched with a warning instead of being wiped. Writes are now
  atomic (temp+rename) with a one-time backup, write **through** symlinks (dotfiles-safe), and
  preserve the file's permissions.
- **Routing reliability:** routing no longer silently turns itself off on window reload (deactivate
  no longer unwires); the terminal env-collection is non-persistent and rebuilt from settings on
  activate; **Disable Routing** clears only the keys we manage (never your `ANTHROPIC_API_KEY`),
  warns that already-running sessions keep routing until restarted, and reports if it couldn't
  revert settings.json. The status bar now shows routing **ON/OFF** (and warns if on-but-gateway-down).
  Enable/Disable Routing now offer a one-click **Reload Window** so the change applies to the
  already-open Claude Code panel immediately (it captures routing at session start, so a live
  session can't be re-routed without a reload).
- **Security:** the BYOK key is no longer written into VS Code's synced env-collection — it stays in
  `~/.decompute/claude.key` (chmod 600, enforced even on a pre-existing file). The `vscode://activate`
  deep link validates the key shape and exact path.
- **Robustness:** API calls time out (no more hung status bar); `pollSeconds` is coerced/validated
  and the poller restarts on change; health-check before enabling routing.
- **Packaging:** `MIT` license id; the VSIX no longer ships tests/internal docs; CI gates releases on
  the settings-safety test suite. Unit tests added for the data-loss/symlink/permission paths.

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
