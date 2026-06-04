# Releasing the Decompute VS Code extension

The build + package is automated and proven (`npm run package` → `decompute-<version>.vsix`).
What remains for a public release needs accounts/credentials that only the org owner can create.

## One-time setup (org owner)

1. **Create a Marketplace publisher** — https://marketplace.visualstudio.com/manage. The
   publisher id must match `"publisher"` in `package.json` (currently `decompute`).
2. **Create a PAT** for that publisher — Azure DevOps → User Settings → Personal Access Tokens →
   scope **Marketplace: Manage**. Save it as the GitHub repo secret `VSCE_PAT`.
3. (Optional, for Cursor/VSCodium) **Open VSX**: create a token at https://open-vsx.org and save
   it as `OVSX_PAT`.
4. **Add an icon**: drop a 128×128 `icon.png` in this folder and add `"icon": "icon.png"` to
   `package.json` (the Marketplace shows a default otherwise).

## Cutting a release

```bash
cd apps/vscode-extension
npm version patch              # bumps package.json version
git commit -am "vscode: vX.Y.Z" && git tag vscode-vX.Y.Z && git push --tags
```

The `Publish VS Code extension` GitHub Action builds, packages, uploads the `.vsix` artifact, and
publishes to the Marketplace (and Open VSX if `OVSX_PAT` is set).

## Manual publish (no CI)

```bash
npm run package                                  # -> decompute-X.Y.Z.vsix
npx @vscode/vsce login decompute                 # paste the PAT once
npm run publish                                  # vsce publish
```

## Local install / test (no Marketplace needed)

```bash
npm run package
code --install-extension decompute-0.1.0.vsix    # or: VS Code → Extensions → ⋯ → Install from VSIX
```

Then: ensure a gateway is running (`decompute init` then it auto-starts), open a terminal — the
status bar shows `Decompute …/100`, run `claude`, and traffic routes through the gateway. Hit the
free limit to see the upgrade modal; `Decompute: Activate Pro License` to go Pro.

## Pre-publish checklist

- [ ] `icon.png` (128×128) added and referenced
- [ ] `publisher` matches the Marketplace publisher id
- [ ] `VSCE_PAT` (and optionally `OVSX_PAT`) set as repo secrets
- [ ] README screenshots/gif (Marketplace renders the README)
- [ ] bump version + tag `vscode-vX.Y.Z`
