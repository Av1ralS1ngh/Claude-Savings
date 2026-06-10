import * as vscode from "vscode";
import * as os from "os";
import * as fs from "fs";
import * as path from "path";
import { execFile } from "child_process";
import { randomUUID } from "crypto";
import {
  DEFAULT_GATEWAY_URL,
  normalizeGatewayUrl,
  parseHttpUrl,
  setupCommandForGateway,
  validateGatewayInput,
} from "./url";
import { planWire, planUnwire, readFileOrNull, writeFileAtomic, backupOnce } from "./settings";
import { cliBinCandidates, configFiles, claudeKeyFile, resolveInstallCommand, shQuote } from "./setup";

const CONFIG_DIR = path.join(os.homedir(), ".decompute");
const DEVICE_ID_FILE = path.join(CONFIG_DIR, "device.id");
const CLAUDE_SETTINGS = path.join(os.homedir(), ".claude", "settings.json");
const SETUP_URL = "https://decompute.ai";
const UPGRADE_URL = "https://decompute.ai/pro";

function cfg<T>(key: string, dflt: T): T {
  return vscode.workspace.getConfiguration("decompute").get<T>(key, dflt);
}

function gatewayUrl(): string {
  return normalizeGatewayUrl(cfg("gatewayUrl", DEFAULT_GATEWAY_URL));
}

function optionalConfigUrl(key: string): string {
  return normalizeGatewayUrl(cfg(key, ""), "");
}

/** True only if Claude Code is ACTUALLY routed through our gateway — i.e. ~/.claude/settings.json
 * has env.ANTHROPIC_BASE_URL pointing at the configured (or local) gateway. The status bar gates
 * on this so it never shows "Pro"/quota while routing is off; a Pro license alone doesn't mean
 * Decompute is in the request path. */
function claudeCodeRoutedToGateway(): boolean {
  try {
    const p = path.join(os.homedir(), ".claude", "settings.json");
    const base = JSON.parse(fs.readFileSync(p, "utf8"))?.env?.ANTHROPIC_BASE_URL;
    if (typeof base !== "string" || !base) return false;
    const norm = normalizeGatewayUrl(base, "");
    return [gatewayUrl(), optionalConfigUrl("localGatewayUrl")].filter(Boolean).includes(norm);
  } catch {
    return false;
  }
}

let cachedDeviceId: string | undefined;
function loadDeviceId(): string {
  if (cachedDeviceId) return cachedDeviceId;
  try {
    const v = fs.readFileSync(DEVICE_ID_FILE, "utf8").trim();
    if (v) return (cachedDeviceId = v);
  } catch {
    // Created lazily below.
  }

  const id = "dev-" + randomUUID().replace(/-/g, "");
  try {
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    fs.writeFileSync(DEVICE_ID_FILE, id + "\n", { mode: 0o600 });
  } catch {
    // Best effort. Cache still keeps the identity stable for this extension host session.
  }
  return (cachedDeviceId = id);
}

async function api<T = any>(p: string, init: RequestInit & { headers?: Record<string, string> } = {}, baseUrl = gatewayUrl()): Promise<T> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 8000);
  try {
    const res = await fetch(baseUrl + p, {
      ...init,
      signal: ctrl.signal,
      headers: {
        "content-type": "application/json",
        "x-decompute-csrf": "vscode",
        "x-decompute-device": loadDeviceId(),
        ...(init.headers || {}),
      },
    });
    const text = await res.text();
    let data: any = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = { detail: text.slice(0, 300) };
      }
    }
    if (!res.ok) throw new Error(data.detail || data?.error?.message || `HTTP ${res.status}`);
    return data as T;
  } finally {
    clearTimeout(t);
  }
}

async function healthCheck(baseUrl: string): Promise<{ ok: boolean; detail: string }> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 3000);
  try {
    const res = await fetch(baseUrl + "/health", { signal: ctrl.signal });
    if (!res.ok) return { ok: false, detail: `HTTP ${res.status}` };
    let detail = "health check passed";
    try {
      const data: any = await res.json();
      const env = data?.environment ? `env=${data.environment}` : "";
      const version = data?.version ? `version=${data.version}` : "";
      detail = [env, version].filter(Boolean).join("; ") || detail;
    } catch {
      // A 2xx response is enough for this lightweight check.
    }
    return { ok: true, detail };
  } catch (e: any) {
    return { ok: false, detail: e?.message || String(e) };
  } finally {
    clearTimeout(t);
  }
}

let statusBar: vscode.StatusBarItem;
let timer: ReturnType<typeof setInterval> | undefined;
let lastPromptAt = 0;

function pollMs(): number {
  const raw = Number(cfg<unknown>("pollSeconds", 60));
  const secs = Number.isFinite(raw) && raw > 0 ? raw : 60;
  return Math.max(15, secs) * 1000;
}

function restartPolling(): void {
  if (timer) clearInterval(timer);
  timer = setInterval(() => void refresh(), pollMs());
}

export function activate(context: vscode.ExtensionContext): void {
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.command = "decompute.status";
  context.subscriptions.push(statusBar);

  context.subscriptions.push(
    vscode.commands.registerCommand("decompute.setup", () => runSetupWizard()),
    vscode.commands.registerCommand("decompute.getStarted", () => openSetup()),
    vscode.commands.registerCommand("decompute.activate", () => activateLicense()),
    vscode.commands.registerCommand("decompute.upgrade", () => upgrade()),
    vscode.commands.registerCommand("decompute.status", () => showStatus()),
    vscode.commands.registerCommand("decompute.showUsage", () => showUsagePanel()),
    vscode.commands.registerCommand("decompute.configureGateway", () => configureGatewayUrl()),
    vscode.commands.registerCommand("decompute.copyClaudeSetup", () => copyClaudeSetupCommand()),
    vscode.commands.registerCommand("decompute.enableRouting", () => enableRouting()),
    vscode.commands.registerCommand("decompute.disableRouting", () => disableRouting()),
    vscode.commands.registerCommand("decompute.checkLocalGateway", () => checkLocalGateway()),
    vscode.commands.registerCommand("decompute.openDashboard", () => openDashboard()),
  );

  context.subscriptions.push(
    vscode.window.registerUriHandler({
      handleUri(uri: vscode.Uri) {
        if (uri.path.replace(/^\/+/, "") !== "activate") return;
        const key = new URLSearchParams(uri.query).get("key");
        if (key && /^DCMP-[A-Za-z0-9_-]{8,}$/.test(key)) void doActivate(key);
      },
    })
  );

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("decompute")) {
        void refresh();
        if (e.affectsConfiguration("decompute.pollSeconds")) restartPolling();
      }
    })
  );

  void maybeOnboard(context);
  void refresh();
  restartPolling();
  context.subscriptions.push({ dispose: () => timer && clearInterval(timer) });
}

export function deactivate(): void {
  if (timer) clearInterval(timer);
}

// ── First-run onboarding ─────────────────────────────────────────────────────
// The extension is a controller for the `decompute` CLI/gateway (the "claude-coder" package). A
// fresh user who installed only the VSIX has no CLI yet, so the extension detects that and walks
// them through install → connect key → route → verify. See runSetupWizard.

async function maybeOnboard(context: vscode.ExtensionContext): Promise<void> {
  const installed = (await whichCli()) !== null;
  const ready = installed && cliConfigured();
  if (!ready) {
    // Auto-popup once per PHASE so "Later" doesn't silence us forever: if the user installs the CLI
    // later (phase install → configure), we prompt again. The status bar ("Decompute: Set up")
    // nudges persistently in between, so this is discovery, not nagging.
    const phase = installed ? "configure" : "install";
    if (context.globalState.get("decompute.onboard.dismissedAt") === phase) return;
    const pick = await vscode.window.showInformationMessage(
      installed
        ? "Decompute is installed but not connected to Claude Code yet. Finish setup?"
        : "Welcome to Decompute! A one-time setup installs the helper and routes Claude Code through the compression gateway.",
      "Set Up Decompute",
      "Later"
    );
    if (pick === "Set Up Decompute") await runSetupWizard();
    else if (pick === "Later") void context.globalState.update("decompute.onboard.dismissedAt", phase);
    return;
  }
  // Installed + configured: show the original one-time tip.
  if (context.globalState.get("decompute.welcomed.thinController")) return;
  void context.globalState.update("decompute.welcomed.thinController", true);
}

// ── decompute CLI detection ──────────────────────────────────────────────────
let cachedBin: string | undefined;

/** Cheap, synchronous "is the CLI present?" for the hot refresh path. Scans the canonical candidate
 *  shims FIRST (so a rebuilt venv / repointed shim is re-detected), falling back to a previously
 *  resolved path; clears the cache when nothing is found so it can't serve a stale location. */
function cliInstalledSync(): boolean {
  for (const c of cliBinCandidates()) {
    try {
      if (fs.existsSync(c)) {
        cachedBin = c;
        return true;
      }
    } catch {
      // ignore unreadable candidate
    }
  }
  try {
    if (cachedBin && fs.existsSync(cachedBin)) return true;
  } catch {
    // fall through
  }
  cachedBin = undefined;
  return false;
}

/** Resolve the absolute `decompute` path: known shim locations first (fast, PATH-independent), then
 *  a login-shell `command -v` in the user's own shell so a custom-PATH install is still found.
 *  Caches the result. POSIX-only — on Windows there's nothing to probe (the installer is bash). */
function whichCli(): Promise<string | null> {
  for (const c of cliBinCandidates()) {
    try {
      if (fs.existsSync(c)) {
        cachedBin = c;
        return Promise.resolve(c);
      }
    } catch {
      // ignore
    }
  }
  if (process.platform === "win32") return Promise.resolve(null);
  return new Promise((resolve) => {
    // The user's login shell so their PATH (where the shim is linked) is loaded.
    const shell = process.env.SHELL || "/bin/sh";
    execFile(shell, ["-lc", "command -v decompute"], { timeout: 5000 }, (err, stdout) => {
      const p = String(stdout || "").trim();
      if (!err && p && fs.existsSync(p)) {
        cachedBin = p;
        resolve(p);
      } else {
        resolve(null);
      }
    });
  });
}

/** init has run at least once (writes ~/.decompute/config.toml as its final step). */
function cliConfigured(): boolean {
  return configFiles().some((f) => {
    try {
      return fs.existsSync(f);
    } catch {
      return false;
    }
  });
}

/** A BYOK Claude key is connected (~/.decompute/claude.key, chmod 600). */
function claudeKeyPresent(): boolean {
  try {
    return fs.existsSync(claudeKeyFile());
  } catch {
    return false;
  }
}

/** Poll a predicate until true or timeout; honors a cancellation token. Used to detect when an
 *  interactive terminal step (install / init) has finished, since terminals give no completion event. */
async function pollUntil(
  pred: () => boolean | Promise<boolean>,
  timeoutMs: number,
  token?: vscode.CancellationToken
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (token?.isCancellationRequested) return false;
    try {
      if (await pred()) return true;
    } catch {
      // transient fs error — keep polling
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
  if (token?.isCancellationRequested) return false; // cancellation is authoritative — no extra probe
  try {
    return (await pred()) === true;
  } catch {
    return false;
  }
}

function isLoopbackGateway(url: string): boolean {
  try {
    const h = new URL(url).hostname;
    return h === "localhost" || h === "127.0.0.1" || h === "0.0.0.0" || h === "::1" || h === "[::1]";
  } catch {
    return false;
  }
}

// ── Guided setup wizard ──────────────────────────────────────────────────────
// Step 1 install (terminal) → Step 2 connect key via the interactive `decompute init` wizard
// (its OWN terminal — we never send follow-up commands into it) → Step 3 route Claude Code
// IN-PROCESS (write ~/.claude/settings.json directly, so nothing collides with init's stdin) →
// Step 4 verify over the network (gateway /health), then show `doctor` in a fresh terminal. Each
// step says exactly what the user must do; success is only claimed once the gateway is reachable.
async function runSetupWizard(): Promise<void> {
  const gw = gatewayUrl();
  // Never interpolate an unparseable gateway URL into a terminal command (defense-in-depth).
  if (!parseHttpUrl(gw)) {
    const pick = await vscode.window.showErrorMessage(
      `Decompute: the gateway URL "${gw}" isn't a valid http(s) URL. Fix it, then run setup again.`,
      "Set Gateway URL"
    );
    if (pick === "Set Gateway URL") await configureGatewayUrl();
    return;
  }
  let bin = await whichCli();

  // STEP 1 — install the CLI.
  if (!bin) {
    if (process.platform === "win32") {
      const pick = await vscode.window.showWarningMessage(
        "The Decompute CLI installs on macOS/Linux. On Windows, install it inside WSL, then reopen VS Code connected to WSL.",
        "Open Docs"
      );
      if (pick === "Open Docs") void vscode.env.openExternal(vscode.Uri.parse(SETUP_URL));
      return;
    }
    // Don't run an installer command from an untrusted workspace (the install command is normally a
    // user/machine setting, but trust is the backstop). `isTrusted` may be undefined on old hosts.
    if (vscode.workspace.isTrusted === false) {
      vscode.window.showWarningMessage("Open this folder in a trusted window to install the Decompute CLI (setup runs an installer command).");
      return;
    }
    const defaultCmd = resolveInstallCommand("");
    const configured = String(cfg<string>("cliInstallCommand", "") || "").trim();
    const installCmd = configured || defaultCmd;
    const isCustom = !!configured && configured !== defaultCmd;
    const choice = await vscode.window.showInformationMessage(
      "Set up Decompute — Step 1 of 4: install the helper",
      {
        modal: true,
        detail:
          "Decompute needs a one-time command-line helper (the `decompute` command). It runs the " +
          "compression gateway that Claude Code talks to.\n\n" +
          (isCustom
            ? "⚠ Using a CUSTOM install command from your settings (not the default Decompute installer):\n\n"
            : "I'll run this in a terminal:\n\n") +
          `  ${installCmd}\n\n` +
          "Watch the terminal until it prints “✓ installed” — I'll detect it and continue automatically.",
      },
      "Install Now",
      "Edit Command"
    );
    if (!choice) return;
    let cmd = installCmd;
    if (choice === "Edit Command") {
      const edited = await vscode.window.showInputBox({
        title: "Decompute install command",
        prompt: "Runs in a terminal to install the `decompute` CLI. Local dev: bash /path/to/decompute/scripts/install.sh",
        value: installCmd,
        ignoreFocusOut: true,
      });
      if (edited === undefined) return;
      cmd = edited.trim();
      if (!cmd) return;
    }
    const term = vscode.window.createTerminal({ name: "Decompute · Install" });
    term.show();
    term.sendText(cmd, true);
    bin = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Installing the Decompute CLI — watch the terminal…", cancellable: true },
      async (_p, token) => ((await pollUntil(async () => (await whichCli()) !== null, 180_000, token)) ? whichCli() : null)
    );
    if (!bin) {
      const pick = await vscode.window.showWarningMessage(
        "I can't find the `decompute` command yet. If the installer finished, open a new terminal (or add ~/.local/bin to your PATH), then run setup again.",
        "Try Again",
        "Open Docs"
      );
      if (pick === "Try Again") return runSetupWizard();
      if (pick === "Open Docs") void vscode.env.openExternal(vscode.Uri.parse(SETUP_URL));
      return;
    }
    vscode.window.showInformationMessage("✓ Decompute CLI installed.");
  }

  // STEP 2 — connect the BYOK Claude key via the interactive `init` wizard, in its OWN terminal.
  // We gate completion on config.toml (init writes it as part of finishing) and NEVER type anything
  // into this terminal — `init` keeps prompting after the config write (start gateway? wrapper?),
  // so any injected command would be eaten by its stdin. The remaining steps run in-process.
  if (!cliConfigured()) {
    const go = await vscode.window.showInformationMessage(
      "Set up Decompute — Step 2 of 4: connect your Claude key (terminal wizard)",
      {
        modal: true,
        detail:
          "A terminal will run the Decompute setup wizard. Answer its prompts — just press Enter to accept each [recommended] default:\n\n" +
          "  1. “Upstream Claude mode” → Enter for [1] Anthropic API key.\n" +
          "  2. “Anthropic API key (sk-ant-…)” → paste your key (hidden) and press Enter. It stays on this machine.\n" +
          "  3. “Privacy mode” → Enter for [1].\n" +
          "  4. “CCR” → Enter for [1].\n" +
          "  5. “Start gateway after setup?” → Enter for Yes.\n" +
          "  6. “Configure Claude Code wrapper?” → Enter for Yes.\n\n" +
          "When the terminal returns to your normal shell prompt, it's done — I'll take it from there.",
      },
      "Open Setup Wizard"
    );
    if (!go) return;
    const initTerm = vscode.window.createTerminal({ name: "Decompute · Setup wizard" });
    initTerm.show();
    initTerm.sendText(`${shQuote(bin)} init --base-url ${shQuote(gw)}`, true);
    const configured = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Setup wizard running — paste your sk-ant-… key in the terminal…", cancellable: true },
      async (_p, token) => pollUntil(() => cliConfigured(), 600_000, token)
    );
    if (!configured) {
      vscode.window.showWarningMessage(
        "The setup wizard isn't finished yet. Complete it in the terminal (paste your sk-ant-… key, accept the defaults), then run “Decompute: Set Up” again."
      );
      return;
    }
    // init finished, but it only writes a key if one was entered. No key ⇒ compression can't reach Claude.
    if (!claudeKeyPresent()) {
      const pick = await vscode.window.showWarningMessage(
        "Decompute is configured, but no Claude key was saved — compression can't reach Claude without it.",
        "Add Key in Terminal",
        "Continue Anyway"
      );
      if (pick === "Add Key in Terminal") {
        const t = vscode.window.createTerminal({ name: "Decompute · Add key" });
        t.show();
        t.sendText(`${shQuote(bin)} claude --set-key`, true);
        vscode.window.showInformationMessage("Paste your sk-ant-… key in the terminal, then run “Decompute: Set Up” again.");
        return;
      }
      if (pick !== "Continue Anyway") return;
    } else {
      vscode.window.showInformationMessage("✓ Claude key connected.");
    }
  }

  // STEP 3 — route Claude Code in THIS editor, in-process (no terminal → no stdin collision).
  const routing = applyRoutingWrite(gw);
  if (routing.status === "unparseable") {
    vscode.window.showWarningMessage("Decompute couldn't route Claude Code: ~/.claude/settings.json isn't valid JSON (left untouched). Fix it, then run “Decompute: Enable Routing”.");
    return;
  }
  if (routing.status === "noread" || routing.status === "error") {
    vscode.window.showErrorMessage(`Decompute couldn't write Claude Code routing (~/.claude/settings.json): ${routing.error}.`);
    return;
  }

  // STEP 4 — verify the gateway is actually reachable over the network (init starts it on "Yes").
  const healthy = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "Verifying the Decompute gateway…", cancellable: true },
    async (_p, token) => pollUntil(async () => (await healthCheck(gw)).ok, 60_000, token)
  );
  await refresh();

  if (healthy && claudeCodeRoutedToGateway()) {
    // Show doctor in a fresh terminal so the user sees the green checks — safe now (gateway up, no init in flight).
    const t = vscode.window.createTerminal({ name: "Decompute · Verify" });
    t.show();
    t.sendText(`${shQuote(bin)} doctor`, true);
    await offerReload("✓ Decompute is set up and the gateway is reachable. Claude Code in this window will route through it after a reload.");
  } else {
    const pick = await vscode.window.showWarningMessage(
      isLoopbackGateway(gw)
        ? `Routing is configured, but the local gateway at ${gw} isn't responding yet. In the setup terminal, answer “Yes” to “Start gateway after setup?”, or run \`decompute claude\` once to start it — then re-run setup to verify.`
        : `Routing is configured, but the gateway at ${gw} isn't responding. Check your network and gateway URL, then run “Decompute: Set Up” again to re-verify.`,
      "Set Gateway URL",
      "Try Again"
    );
    if (pick === "Set Gateway URL") await configureGatewayUrl();
    else if (pick === "Try Again") return runSetupWizard();
  }
}

function openSetup(): void {
  void vscode.env.openExternal(vscode.Uri.parse(SETUP_URL));
}

function openDashboard(): void {
  void vscode.env.openExternal(vscode.Uri.parse(gatewayUrl() + "/dashboard"));
}

async function configureGatewayUrl(): Promise<void> {
  const value = await vscode.window.showInputBox({
    title: "Decompute Gateway URL",
    prompt: "Use the hosted gateway, or an advanced gateway you manage.",
    value: gatewayUrl(),
    ignoreFocusOut: true,
    validateInput: validateGatewayInput,
  });
  if (value === undefined) return;
  const normalized = normalizeGatewayUrl(value);
  const parsed = parseHttpUrl(normalized);
  if (!parsed) return;

  if (parsed.protocol !== "https:") {
    const pick = await vscode.window.showWarningMessage(
      "Use a non-HTTPS gateway only for an advanced local or self-managed setup.",
      { modal: true },
      "Use Advanced Gateway"
    );
    if (pick !== "Use Advanced Gateway") return;
  }

  await vscode.workspace.getConfiguration("decompute").update("gatewayUrl", normalized, vscode.ConfigurationTarget.Global);
  vscode.window.showInformationMessage(`Decompute gateway set to ${normalized}`);
  await refresh();
}

async function copyClaudeSetupCommand(): Promise<void> {
  const command = setupCommandForGateway(gatewayUrl());
  await vscode.env.clipboard.writeText(command);
  const pick = await vscode.window.showInformationMessage(`Copied: ${command}`, "Open Terminal", "Open Dashboard");
  if (pick === "Open Terminal") {
    const terminal = vscode.window.createTerminal({ name: "Decompute setup" });
    terminal.show();
    terminal.sendText(command, false);
  } else if (pick === "Open Dashboard") {
    openDashboard();
  }
}

async function offerReload(message: string): Promise<void> {
  // Claude Code reads ANTHROPIC_BASE_URL at session start, so an already-open panel keeps its old
  // routing until the window reloads. Offer a one-click reload to apply immediately.
  const pick = await vscode.window.showWarningMessage(message, "Reload Window", "Later");
  if (pick === "Reload Window") await vscode.commands.executeCommand("workbench.action.reloadWindow");
}

/** Write the gateway routing block into ~/.claude/settings.json (no UI). Shared by the Enable
 *  Routing command and the setup wizard so routing is applied IN-PROCESS — never by sending a
 *  command into a terminal that may still host the interactive `init` wizard. */
function applyRoutingWrite(gw: string): { status: "ok" | "noread" | "unparseable" | "error"; error?: string } {
  let text: string | null;
  try {
    text = readFileOrNull(CLAUDE_SETTINGS);
  } catch (e: any) {
    return { status: "noread", error: e?.message || String(e) };
  }
  const plan = planWire(text, gw, `x-decompute-device: ${loadDeviceId()}`);
  if (plan.status === "unparseable") return { status: "unparseable" };
  if (plan.text !== null) {
    try {
      backupOnce(CLAUDE_SETTINGS, text);
      writeFileAtomic(CLAUDE_SETTINGS, plan.text);
    } catch (e: any) {
      return { status: "error", error: e?.message || String(e) };
    }
  }
  return { status: "ok" };
}

async function enableRouting(): Promise<void> {
  const gw = gatewayUrl();
  const r = applyRoutingWrite(gw);
  if (r.status === "noread") {
    vscode.window.showWarningMessage(`Decompute: couldn't read ~/.claude/settings.json (${r.error}); routing not enabled.`);
    return;
  }
  if (r.status === "unparseable") {
    vscode.window.showWarningMessage("Decompute: ~/.claude/settings.json isn't valid JSON, so routing was NOT enabled (your file was left untouched). Fix it, then try again.");
    return;
  }
  if (r.status === "error") {
    vscode.window.showErrorMessage(`Decompute: failed to update ~/.claude/settings.json: ${r.error}`);
    return;
  }
  await refresh();
  await offerReload(`Decompute routing enabled → ${gw}. An already-open Claude Code panel won't use it until the window reloads — reload now?`);
}

async function disableRouting(): Promise<void> {
  let text: string | null;
  try {
    text = readFileOrNull(CLAUDE_SETTINGS);
  } catch (e: any) {
    vscode.window.showWarningMessage(`Decompute: couldn't read ~/.claude/settings.json (${e?.message || e}).`);
    return;
  }
  const plan = planUnwire(text);
  if (plan.status === "unparseable") {
    vscode.window.showWarningMessage("Decompute: ~/.claude/settings.json isn't valid JSON; left untouched. Remove the env ANTHROPIC_BASE_URL/ANTHROPIC_CUSTOM_HEADERS entries manually.");
    return;
  }
  if (plan.text !== null) {
    try {
      writeFileAtomic(CLAUDE_SETTINGS, plan.text);
    } catch (e: any) {
      vscode.window.showErrorMessage(`Decompute: failed to update ~/.claude/settings.json: ${e?.message || e}`);
      return;
    }
  }
  await refresh();
  await offerReload("Decompute routing disabled. Already-running Claude Code sessions keep routing until the window reloads — reload now to go direct?");
}

async function checkLocalGateway(): Promise<void> {
  let localUrl = optionalConfigUrl("localGatewayUrl");
  if (!localUrl) {
    const pick = await vscode.window.showInformationMessage(
      "No advanced local gateway URL is configured.",
      "Set Local Gateway URL"
    );
    if (pick !== "Set Local Gateway URL") return;
    const value = await vscode.window.showInputBox({
      title: "Advanced Local Gateway URL",
      prompt: "Enter the URL for a separately installed Decompute Local Gateway.",
      ignoreFocusOut: true,
      validateInput: validateGatewayInput,
    });
    if (value === undefined) return;
    localUrl = normalizeGatewayUrl(value, "");
    await vscode.workspace.getConfiguration("decompute").update("localGatewayUrl", localUrl, vscode.ConfigurationTarget.Global);
  }

  const result = await healthCheck(localUrl);
  if (result.ok) {
    vscode.window.showInformationMessage(`Local Decompute Gateway is reachable at ${localUrl} (${result.detail}).`);
  } else {
    vscode.window.showWarningMessage(`Local Decompute Gateway is not reachable at ${localUrl}: ${result.detail}`);
  }
}

function isPro(s: any): boolean {
  return s?.plan === "pro" || s?.tier === "pro" || s?.license_status === "active";
}

function quota(status: any): { remaining?: number; limit?: number } {
  const q = status?.quota || {};
  return {
    remaining: firstNumber(q.remaining, status?.quota_remaining, status?.free_quota_remaining),
    limit: firstNumber(q.limit, status?.quota_limit, status?.free_quota_limit),
  };
}

function firstNumber(...values: unknown[]): number | undefined {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return undefined;
}

function planSummary(status: any): string {
  if (isPro(status)) return "Plan: Pro";
  const q = quota(status);
  if (q.remaining !== undefined && q.limit !== undefined) return `Plan: Free (${q.remaining}/${q.limit} compressions left)`;
  return "Plan: Free";
}

async function refresh(): Promise<void> {
  // Not installed/configured yet → make the status bar a one-click entry into the setup wizard,
  // instead of showing a (misleading) gateway-unreachable state.
  if (!cliInstalledSync() || !cliConfigured()) {
    statusBar.text = "$(cloud-download) Decompute: Set up";
    statusBar.tooltip = cliInstalledSync()
      ? "Decompute CLI is installed but not configured. Click to finish setup."
      : "Decompute isn't set up yet. Click to install the CLI and connect Claude Code.";
    statusBar.command = "decompute.setup";
    statusBar.show();
    return;
  }
  if (statusBar.command !== "decompute.status") statusBar.command = "decompute.status";

  const routed = claudeCodeRoutedToGateway();
  try {
    const s = await api<any>("/v1/license/status", { method: "GET" });
    const q = quota(s);
    if (!routed) {
      // Routing is OFF — Decompute isn't in the request path, so don't imply it's active by
      // showing Pro/quota. Surface the license in the tooltip only.
      statusBar.text = "$(circle-slash) Decompute off";
      statusBar.tooltip = `Routing OFF — Claude Code is not configured to use the gateway (${gatewayUrl()}).\nRun "Decompute: Copy Setup Command" to route. ${planSummary(s)}`;
    } else if (isPro(s)) {
      statusBar.text = "$(zap) Decompute Pro";
      statusBar.tooltip = `Routing ON → ${gatewayUrl()}\n${planSummary(s)}`;
    } else if (q.remaining !== undefined && q.limit !== undefined) {
      statusBar.text = `$(zap) Decompute ${q.remaining}/${q.limit}`;
      statusBar.tooltip = `Routing ON → ${gatewayUrl()}\n${planSummary(s)}`;
    } else {
      statusBar.text = "$(zap) Decompute";
      statusBar.tooltip = `Routing ON → ${gatewayUrl()}\n${planSummary(s)}`;
    }
    statusBar.show();
    if (routed && s.ui && s.ui.show_modal) void promptUpgrade(s);
  } catch {
    statusBar.text = "$(cloud-off) Decompute";
    statusBar.tooltip = `Decompute Gateway is not reachable at ${gatewayUrl()}. Click for setup and configuration.`;
    statusBar.show();
  }
}

async function promptUpgrade(s: any): Promise<void> {
  const now = Date.now();
  if (now - lastPromptAt < 10 * 60 * 1000) return;
  lastPromptAt = now;
  const msg = (s.ui && s.ui.message) || "You reached the free compression limit.";
  const pick = await vscode.window.showWarningMessage(msg, "Upgrade", "Activate License", "Usage");
  if (pick === "Upgrade") await upgrade(s.upgrade_url);
  else if (pick === "Activate License") await activateLicense();
  else if (pick === "Usage") await showUsagePanel();
}

async function upgrade(url?: string): Promise<void> {
  let target = url;
  if (!target) {
    try {
      const s = await api<any>("/v1/license/status", { method: "GET" });
      target = s.upgrade_url;
    } catch {
      // Fall back to the public purchase page.
    }
  }
  await vscode.env.openExternal(vscode.Uri.parse(target || UPGRADE_URL));
}

async function activateLicense(): Promise<void> {
  const key = await vscode.window.showInputBox({
    prompt: "Paste your Decompute license key.",
    ignoreFocusOut: true,
  });
  if (key) await doActivate(key.trim());
}

async function doActivate(key: string): Promise<void> {
  try {
    await api("/v1/license/activate", { method: "POST", body: JSON.stringify({ license_key: key }) });
    vscode.window.showInformationMessage("Decompute license activated on this device.");
    await refresh();
  } catch (e: any) {
    vscode.window.showErrorMessage("Activation failed: " + (e?.message || String(e)));
  }
}

async function showStatus(): Promise<void> {
  try {
    const s = await api<any>("/v1/license/status", { method: "GET" });
    const actions = [
      claudeCodeRoutedToGateway() ? "Disable Routing" : "Enable Routing",
      "Usage and Savings",
      "Copy Setup Command",
      "Open Dashboard",
      "Set Gateway URL",
      ...(isPro(s) ? [] : ["Upgrade", "Activate License"]),
      "Run Setup",
      "Check Local Gateway",
    ];
    const pick = await vscode.window.showInformationMessage(
      `Decompute Gateway: ${gatewayUrl()} · Routing ${claudeCodeRoutedToGateway() ? "ON" : "OFF"}\n${planSummary(s)}`,
      ...actions
    );
    await handleStatusPick(pick, s);
  } catch (e: any) {
    const pick = await vscode.window.showWarningMessage(
      `Could not reach Decompute Gateway at ${gatewayUrl()}: ${e?.message || String(e)}`,
      "Run Setup",
      "Set Gateway URL",
      "Copy Setup Command",
      "Open Dashboard",
      "Check Local Gateway"
    );
    await handleStatusPick(pick, undefined);
  }
}

async function handleStatusPick(pick: string | undefined, status: any): Promise<void> {
  if (pick === "Enable Routing") await enableRouting();
  else if (pick === "Disable Routing") await disableRouting();
  else if (pick === "Usage and Savings") await showUsagePanel();
  else if (pick === "Copy Setup Command") await copyClaudeSetupCommand();
  else if (pick === "Open Dashboard") openDashboard();
  else if (pick === "Set Gateway URL") await configureGatewayUrl();
  else if (pick === "Upgrade") await upgrade(status?.upgrade_url);
  else if (pick === "Activate License") await activateLicense();
  else if (pick === "Run Setup") await runSetupWizard();
  else if (pick === "Check Local Gateway") await checkLocalGateway();
}

async function showUsagePanel(): Promise<void> {
  const errors: string[] = [];
  let status: any = undefined;
  let usage: any = undefined;

  try {
    status = await api<any>("/v1/license/status", { method: "GET" });
  } catch (e: any) {
    errors.push(`License status unavailable: ${e?.message || String(e)}`);
  }
  try {
    usage = await api<any>("/v1/usage", { method: "GET" });
  } catch (e: any) {
    errors.push(`Usage unavailable: ${e?.message || String(e)}`);
  }

  const panel = vscode.window.createWebviewPanel(
    "decomputeUsage",
    "Decompute Usage",
    vscode.ViewColumn.One,
    { enableScripts: false }
  );
  panel.webview.html = renderUsageHtml(panel.webview, { gateway: gatewayUrl(), status, usage, errors });
}

function numberFrom(obj: any, keys: string[]): number | undefined {
  if (!obj || typeof obj !== "object") return undefined;
  for (const key of keys) {
    const value = obj[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return undefined;
}

function formatNumber(value: number | undefined): string {
  return value === undefined ? "-" : new Intl.NumberFormat("en-US").format(value);
}

function formatUsd(value: number | undefined): string {
  return value === undefined ? "-" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(value);
}

function escapeHtml(value: unknown): string {
  const chars: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(value ?? "").replace(/[&<>"']/g, (ch) => chars[ch]);
}

function renderUsageHtml(webview: vscode.Webview, data: { gateway: string; status: any; usage: any; errors: string[] }): string {
  const q = quota(data.status);
  const requests = numberFrom(data.usage, ["requests", "total_requests", "request_count"]);
  const before = numberFrom(data.usage, ["tokens_before", "input_tokens_before", "total_tokens_before"]);
  const after = numberFrom(data.usage, ["tokens_after", "input_tokens_after", "total_tokens_after"]);
  const saved = numberFrom(data.usage, ["tokens_saved", "total_tokens_saved"]);
  const savings = numberFrom(data.usage, ["estimated_savings_usd", "savings_usd", "total_savings_usd"]);
  const errorHtml = data.errors.length
    ? `<div class="notice">${data.errors.map(escapeHtml).join("<br>")}</div>`
    : "";

  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline';">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Decompute Usage</title>
  <style>
    body { margin: 0; padding: 24px; color: var(--vscode-foreground); background: var(--vscode-editor-background); font-family: var(--vscode-font-family); }
    h1 { margin: 0 0 6px; font-size: 22px; font-weight: 650; }
    .muted { color: var(--vscode-descriptionForeground); }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-top: 20px; }
    .card { border: 1px solid var(--vscode-panel-border); border-radius: 8px; padding: 14px; background: var(--vscode-sideBar-background); }
    .label { color: var(--vscode-descriptionForeground); font-size: 12px; margin-bottom: 8px; }
    .value { font-size: 20px; font-weight: 650; overflow-wrap: anywhere; }
    .notice { margin-top: 16px; padding: 12px; border: 1px solid var(--vscode-inputValidation-warningBorder); border-radius: 8px; color: var(--vscode-inputValidation-warningForeground); background: var(--vscode-inputValidation-warningBackground); }
  </style>
</head>
<body>
  <h1>Decompute Usage</h1>
  <div class="muted">${escapeHtml(data.gateway)}</div>
  ${errorHtml}
  <section class="grid">
    <div class="card"><div class="label">Plan</div><div class="value">${escapeHtml(isPro(data.status) ? "Pro" : "Free")}</div></div>
    <div class="card"><div class="label">Quota</div><div class="value">${escapeHtml(q.remaining !== undefined && q.limit !== undefined ? `${q.remaining}/${q.limit}` : "-")}</div></div>
    <div class="card"><div class="label">Requests</div><div class="value">${escapeHtml(formatNumber(requests))}</div></div>
    <div class="card"><div class="label">Tokens Before</div><div class="value">${escapeHtml(formatNumber(before))}</div></div>
    <div class="card"><div class="label">Tokens After</div><div class="value">${escapeHtml(formatNumber(after))}</div></div>
    <div class="card"><div class="label">Tokens Saved</div><div class="value">${escapeHtml(formatNumber(saved))}</div></div>
    <div class="card"><div class="label">Estimated Savings</div><div class="value">${escapeHtml(formatUsd(savings))}</div></div>
  </section>
</body>
</html>`;
}
