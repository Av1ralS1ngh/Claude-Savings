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
const LICENSE_FILE = path.join(CONFIG_DIR, "license.dcmp");
const CLAUDE_SETTINGS = path.join(os.homedir(), ".claude", "settings.json");
const CLAUDE_CODE_EXT_ID = "anthropic.claude-code"; // the Claude Code extension Decompute sits in front of
const LOCAL_BIN = path.join(os.homedir(), ".local", "bin"); // where the installer links the shims
const SHELL_RC_FILES = [path.join(os.homedir(), ".zprofile"), path.join(os.homedir(), ".bash_profile")];
const SETUP_URL = "https://decompute.run";
const UPGRADE_URL = "https://decompute.run/pro";
// Stripe Payment Link for the Pro plan. We append ?client_reference_id=<deviceId> so the purchase is
// tagged with the buyer's machine; the license issuer (Worker webhook) reads it back to mint a license
// bound to THIS device, which "Activate Pro" then fetches. Override via the `decompute.checkoutUrl` setting.
const PRO_CHECKOUT_URL = "https://buy.stripe.com/14A5kD1twdNifow5te1wY01";
// Deployed license Worker (issues/serves the signed license). Override via `decompute.licenseApiUrl`.
const LICENSE_API_URL = "https://decompute-checkout.xcomp-decompute.workers.dev";

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
    vscode.commands.registerCommand("decompute.activatePro", () => activatePro()),
    vscode.commands.registerCommand("decompute.upgrade", () => upgrade()),
    vscode.commands.registerCommand("decompute.status", () => showStatus()),
    vscode.commands.registerCommand("decompute.showUsage", () => showUsagePanel()),
    vscode.commands.registerCommand("decompute.configureGateway", () => configureGatewayUrl()),
    vscode.commands.registerCommand("decompute.copyClaudeSetup", () => copyClaudeSetupCommand()),
    vscode.commands.registerCommand("decompute.enableRouting", () => enableRouting()),
    vscode.commands.registerCommand("decompute.disableRouting", () => disableRouting()),
    vscode.commands.registerCommand("decompute.checkLocalGateway", () => checkLocalGateway()),
    vscode.commands.registerCommand("decompute.openDashboard", () => openDashboard()),
    vscode.commands.registerCommand("decompute.uninstall", () => uninstallDecompute(context)),
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
  void refreshLicense(); // pick up subscription renewals so an offline Pro license doesn't lapse
  restartPolling();
  context.subscriptions.push({ dispose: () => timer && clearInterval(timer) });
}

export function deactivate(): void {
  if (timer) clearInterval(timer);
  // Belt-and-suspenders: also dispose the status bar item so it can't linger on uninstall+reload
  // (it's in context.subscriptions too, so VS Code disposes it anyway).
  try { statusBar?.dispose(); } catch { /* ignore */ }
}

// ── First-run onboarding ─────────────────────────────────────────────────────
// The extension is a controller for the `decompute` CLI/gateway (the "claude-coder" package). A
// fresh user who installed only the VSIX has no CLI yet, so the extension detects that and walks
// them through install → connect key → route → verify. See runSetupWizard.

let onboardPromptedThisSession = false;

async function maybeOnboard(context: vscode.ExtensionContext): Promise<void> {
  const installed = (await whichCli()) !== null;
  const ready = installed && cliConfigured();
  if (!ready) {
    // Suppress the auto-popup per WINDOW SESSION, not persistently. VS Code globalState is keyed by
    // extension id and SURVIVES uninstall/reinstall, so a persistent "dismissed" flag would silence the
    // wizard forever — including after a clean reinstall (the reported complaint). So we prompt at most
    // once per window; the status bar ("Decompute: Set up") nudges persistently in between; "Don't ask
    // again" is the explicit opt-out that DOES persist.
    if (onboardPromptedThisSession || context.globalState.get("decompute.onboard.neverAsk")) return;
    onboardPromptedThisSession = true;
    const pick = await vscode.window.showInformationMessage(
      installed
        ? "Decompute is installed but not connected to Claude Code yet. Finish setup?"
        : "Welcome to Decompute! A one-time setup installs the helper and routes Claude Code through the compression gateway.",
      "Set Up Decompute",
      "Later",
      "Don't ask again"
    );
    if (pick === "Set Up Decompute") await runSetupWizard();
    else if (pick === "Don't ask again") void context.globalState.update("decompute.onboard.neverAsk", true);
    return;
  }
  // Installed + configured. One-time-per-engine-update nudge: a user who UPDATED the extension is
  // still running their OLD local compression engine until they re-run setup (which now force-
  // reinstalls the wheels). Only nudge users who were ALREADY set up — a fresh install just ran setup
  // with the latest engine, so it doesn't need the prompt.
  const ENGINE_UPDATE_KEY = "decompute.engineUpdate.v0_1_14";
  if (!context.globalState.get(ENGINE_UPDATE_KEY)) {
    void context.globalState.update(ENGINE_UPDATE_KEY, true);
    if (context.globalState.get("decompute.welcomed.thinController")) {
      const pick = await vscode.window.showInformationMessage(
        "Decompute's compression engine was upgraded — attached files, logs, and HTML now compress up to ~99% (previously ~0%). Update your local engine to get it?",
        "Update now",
        "Later"
      );
      if (pick === "Update now") await runSetupWizard();
      return;
    }
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

type ClaudeAuthMode = "claude_subscription" | "anthropic_api" | "enterprise_gateway";

interface ClaudeAuthPick extends vscode.QuickPickItem {
  mode: ClaudeAuthMode;
}

function readClaudeMode(): ClaudeAuthMode | undefined {
  for (const f of configFiles()) {
    try {
      const text = fs.readFileSync(f, "utf8");
      let inClaude = false;
      for (const line of text.split(/\r?\n/)) {
        const section = line.match(/^\s*\[([^\]]+)\]\s*$/);
        if (section) {
          inClaude = section[1] === "claude";
          continue;
        }
        if (!inClaude) continue;
        const m = line.match(/^\s*mode\s*=\s*"([^"]+)"/);
        if (m && ["claude_subscription", "anthropic_api", "enterprise_gateway"].includes(m[1])) {
          return m[1] as ClaudeAuthMode;
        }
      }
    } catch {
      // Missing/unreadable config is handled by the setup flow.
    }
  }
  return undefined;
}

async function chooseClaudeAuthMode(current?: ClaudeAuthMode): Promise<ClaudeAuthMode | undefined> {
  const picks: ClaudeAuthPick[] = [
    {
      mode: "claude_subscription",
      label: "$(account) Use my existing Claude plan",
      description: "Claude Pro, Max, Team, or Enterprise",
      detail: "Best if Claude Code already signs in to your Claude account. Decompute will not ask for or store an API key.",
      picked: current === "claude_subscription",
    },
    {
      mode: "anthropic_api",
      label: "$(key) Use an API key",
      description: "Anthropic Console key",
      detail: "Use this if you pay Anthropic through API billing. Your sk-ant-* key stays on this machine.",
      picked: current === "anthropic_api" || current === "enterprise_gateway",
    },
  ];
  const pick = await vscode.window.showQuickPick(picks, {
    title: "How do you want Claude Code to connect?",
    placeHolder: "Choose the Claude account source Decompute should route.",
    ignoreFocusOut: true,
  });
  return pick?.mode;
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
function claudeCodeInstalled(): boolean {
  return !!vscode.extensions.getExtension(CLAUDE_CODE_EXT_ID);
}

/** Decompute is a controller that sits in front of the Claude Code extension, so it must be installed
 *  first. Returns true to proceed with setup; false to abort (Claude Code missing / still installing). */
async function ensureClaudeCode(): Promise<boolean> {
  if (claudeCodeInstalled()) return true;
  const pick = await vscode.window.showInformationMessage(
    "Decompute needs the Claude Code extension, which isn't installed yet.",
    {
      modal: true,
      detail:
        "Decompute routes Claude Code through a local compression gateway, so the Claude Code extension " +
        "has to be installed before setup. Install it now?",
    },
    "Install Claude Code"
  );
  if (pick !== "Install Claude Code") return false;
  const result = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "Installing Claude Code…", cancellable: true },
    async (_progress, token): Promise<{ ok: boolean; error?: string }> => {
      try {
        await vscode.commands.executeCommand("workbench.extensions.installExtension", CLAUDE_CODE_EXT_ID);
      } catch (e: any) {
        return { ok: false, error: e?.message || String(e) };
      }
      // installExtension can resolve before the extension host registers the extension — poll briefly
      // so setup auto-continues instead of dead-ending on "run setup again".
      for (let i = 0; i < 30 && !token.isCancellationRequested; i++) {
        if (claudeCodeInstalled()) return { ok: true };
        await new Promise((r) => setTimeout(r, 1000));
      }
      return { ok: claudeCodeInstalled() };
    }
  );
  if (result.error) {
    const open = await vscode.window.showWarningMessage(
      `Couldn't install Claude Code automatically (${result.error}). Open it in the Marketplace?`,
      "Open Marketplace"
    );
    if (open === "Open Marketplace") void vscode.commands.executeCommand("workbench.extensions.search", `@id:${CLAUDE_CODE_EXT_ID}`);
    return false;
  }
  if (result.ok) {
    vscode.window.showInformationMessage("Claude Code installed — continuing Decompute setup.");
    return true;
  }
  vscode.window.showInformationMessage("Claude Code is installing. Run “Decompute: Set Up” again once it finishes.");
  return false;
}

async function runSetupWizard(): Promise<void> {
  if (!(await ensureClaudeCode())) return;
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

  const currentMode = readClaudeMode();
  const authMode = await chooseClaudeAuthMode(currentMode);
  if (!authMode) return;
  const usingClaudePlan = authMode === "claude_subscription";
  if (usingClaudePlan && !isLoopbackGateway(gw)) {
    const pick = await vscode.window.showWarningMessage(
      "Using an existing Claude plan is supported through the local Decompute gateway only, so your Claude login stays on this machine.",
      "Use Local Gateway",
      "Set Gateway URL"
    );
    if (pick === "Set Gateway URL") await configureGatewayUrl();
    return;
  }
  if (usingClaudePlan && claudeKeyPresent()) {
    vscode.window.showInformationMessage(
      "I found a saved Anthropic API key from an earlier setup. This Claude-plan setup will ignore it and keep it in case you switch back later."
    );
  }

  // STEP 2 — configure how Claude Code authenticates. API-key mode stays interactive so the secret
  // is typed into the terminal; existing-plan mode is non-interactive and stores no Claude credential.
  const needsInit = !cliConfigured() || readClaudeMode() !== authMode || (!usingClaudePlan && !claudeKeyPresent());
  if (needsInit) {
    const title = usingClaudePlan
      ? "Set up Decompute — Step 2 of 4: use your Claude plan"
      : "Set up Decompute — Step 2 of 4: use an API key";
    const detail = usingClaudePlan
      ? "This uses the Claude account that Claude Code is already signed into (Pro, Max, Team, or Enterprise). Decompute will not ask for an API key and will not store your Claude login."
      : "A terminal will run the Decompute setup wizard. Choose “Use an API key”, paste your sk-ant-* key when prompted, then accept the recommended defaults. The key stays on this machine.";
    const action = usingClaudePlan ? "Use Claude Plan" : "Open Setup Wizard";
    const go = await vscode.window.showInformationMessage(title, { modal: true, detail }, action);
    if (!go) return;
    const initTerm = vscode.window.createTerminal({ name: "Decompute · Setup wizard" });
    initTerm.show();
    const initCmd = usingClaudePlan
      ? `${shQuote(bin)} init --yes --auth-mode claude_subscription --base-url ${shQuote(gw)}`
      : `${shQuote(bin)} init --auth-mode api_key --base-url ${shQuote(gw)}`;
    initTerm.sendText(initCmd, true);
    const configured = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: usingClaudePlan
          ? "Setting up Decompute for your Claude plan…"
          : "Setup wizard running — paste your sk-ant-* key in the terminal…",
        cancellable: true,
      },
      async (_p, token) => pollUntil(
        () => cliConfigured() && readClaudeMode() === authMode && (usingClaudePlan || claudeKeyPresent()),
        600_000,
        token
      )
    );
    if (!configured) {
      vscode.window.showWarningMessage(
        usingClaudePlan
          ? "The setup command hasn't finished yet. Check the Decompute setup terminal, then run “Decompute: Set Up” again."
          : "The setup wizard isn't finished yet. Complete it in the terminal, then run “Decompute: Set Up” again."
      );
      return;
    }
    if (!usingClaudePlan && !claudeKeyPresent()) {
      const pick = await vscode.window.showWarningMessage(
        "Decompute is configured, but no API key was saved.",
        "Add Key in Terminal",
        "Continue Anyway"
      );
      if (pick === "Add Key in Terminal") {
        const t = vscode.window.createTerminal({ name: "Decompute · Add key" });
        t.show();
        t.sendText(`${shQuote(bin)} claude --set-key`, true);
        vscode.window.showInformationMessage("Paste your sk-ant-* key in the terminal, then run “Decompute: Set Up” again.");
        return;
      }
      if (pick !== "Continue Anyway") return;
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

  if (parsed.protocol !== "https:" && !isLoopbackGateway(normalized)) {
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
  const mode = readClaudeMode() || "claude_subscription";
  if (mode === "claude_subscription" && !isLoopbackGateway(gatewayUrl())) {
    vscode.window.showWarningMessage(
      "Existing Claude plan setup is local-only. Set the gateway URL to the local gateway, or run setup and choose “Use an API key”.",
      "Set Gateway URL",
      "Run Setup"
    ).then((pick) => {
      if (pick === "Set Gateway URL") void configureGatewayUrl();
      else if (pick === "Run Setup") void runSetupWizard();
    });
    return;
  }
  const command = setupCommandForGateway(gatewayUrl(), mode);
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
  if (!(await ensureClaudeCode())) return;
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

/** Remove Decompute completely: stop the gateway, delete the CLI/gateway/config + PATH entry, unroute
 *  Claude Code, and clear onboarding state so a future reinstall starts fresh. The marketplace
 *  "Uninstall" only removes the extension, never this footprint — so we expose it as a command. */
async function uninstallDecompute(context: vscode.ExtensionContext): Promise<void> {
  const pick = await vscode.window.showWarningMessage(
    "Remove Decompute completely from this machine?",
    {
      modal: true,
      detail:
        "Stops the gateway and deletes the Decompute CLI, gateway, config and PATH entry (~/.decompute, the " +
        "decompute/decompute-gateway commands, the ~/.zprofile line), and removes Decompute's routing from " +
        "Claude Code (~/.claude/settings.json — backed up first). Your Claude Code key and other settings are " +
        "left untouched.\n\nAfter this, uninstall the “Decompute” extension from the Extensions view to finish.",
    },
    "Remove Decompute"
  );
  if (pick !== "Remove Decompute") return;

  const done: string[] = [];

  // 1. Remove the launchd watchdog FIRST (so it doesn't restart the gateway), then stop the gateway.
  const plist = path.join(os.homedir(), "Library", "LaunchAgents", "run.decompute.gateway.plist");
  try { execFile("launchctl", ["unload", plist], () => undefined); } catch { /* ignore */ }
  try { fs.rmSync(plist, { force: true }); } catch { /* ignore */ }
  try { execFile("pkill", ["-f", "decompute-gateway"], () => undefined); } catch { /* ignore */ }

  // 2. Remove Decompute's routing from Claude Code — touch ONLY our two keys (planUnwire), and surface
  //    it if we couldn't (rather than silently leaving routing on). No backup file is left behind: the
  //    write is atomic and reversible by re-running setup, so a complete uninstall stays clean.
  let routingNote = "";
  try {
    const text = readFileOrNull(CLAUDE_SETTINGS);
    const plan = planUnwire(text);
    if (plan.status === "unparseable") {
      routingNote = " — couldn't auto-remove routing (~/.claude/settings.json isn't valid JSON; remove ANTHROPIC_BASE_URL / ANTHROPIC_CUSTOM_HEADERS yourself)";
    } else if (plan.text !== null) {
      writeFileAtomic(CLAUDE_SETTINGS, plan.text);
      done.push("unrouted Claude Code");
    }
  } catch (e: any) {
    routingNote = ` — couldn't update ~/.claude/settings.json (${e?.message || e})`;
  }

  // 3. Delete the install: ~/.decompute, the shims, and the installer's PATH line.
  try { fs.rmSync(CONFIG_DIR, { recursive: true, force: true }); done.push("removed ~/.decompute"); } catch { /* ignore */ }
  for (const cmd of ["decompute", "decompute-gateway"]) {
    try { fs.rmSync(path.join(LOCAL_BIN, cmd), { force: true }); } catch { /* ignore */ }
  }
  for (const rc of SHELL_RC_FILES) removeInstallerPathLine(rc);
  done.push("removed CLI + PATH entry");

  // 4. Clear onboarding state so a fresh reinstall prompts setup again from scratch.
  await context.globalState.update("decompute.onboard.dismissedAt", undefined); // legacy key
  await context.globalState.update("decompute.onboard.neverAsk", undefined);
  await context.globalState.update("decompute.welcomed.thinController", undefined);
  onboardPromptedThisSession = false;
  cachedBin = undefined; // forget any resolved CLI path

  // Stop polling and remove the status bar item right away.
  if (timer) clearInterval(timer);
  try { statusBar.dispose(); } catch { /* already disposed */ }

  // Uninstall the EXTENSION itself too, so this is one-stop — the user shouldn't also have to remove it
  // from the Extensions view. VS Code finalizes the removal (and drops the status bar) on reload.
  let extNote: string;
  try {
    await vscode.commands.executeCommand("workbench.extensions.uninstallExtension", "decompute.decompute");
    extNote = " The Decompute extension will finish uninstalling when you reload.";
  } catch {
    extNote = " Also remove the “Decompute” extension from the Extensions view, then reload.";
  }
  const reload = await vscode.window.showInformationMessage(
    `Decompute removed (${done.join(", ")})${routingNote}.${extNote}`,
    "Reload Window"
  );
  if (reload === "Reload Window") void vscode.commands.executeCommand("workbench.action.reloadWindow");
}

/** Silently pull the latest signed Pro license from the license Worker, so subscription RENEWALS
 *  propagate and an offline license never lapses while the user keeps paying. No-op when there's no
 *  license yet (nothing to renew) or we're offline (keep the current one). Runs on each activation. */
async function refreshLicense(): Promise<void> {
  const apiUrl = (cfg<string>("licenseApiUrl", "") || LICENSE_API_URL).replace(/\/+$/, "");
  if (!apiUrl) return;
  let current = "";
  try { current = fs.readFileSync(LICENSE_FILE, "utf8").trim(); } catch { return; } // no license → nothing to refresh
  if (!current) return;
  try {
    const r = await fetch(`${apiUrl}/license?device=${encodeURIComponent(loadDeviceId())}`);
    if (r.status !== 200) return;
    const j: any = await r.json();
    const latest = j?.license ? String(j.license).trim() : "";
    if (latest && latest !== current) {
      fs.writeFileSync(LICENSE_FILE, latest + "\n", { mode: 0o600 });
      await refresh();
    }
  } catch { /* offline / worker unreachable — keep the current license */ }
}

/** Strip the marker-guarded PATH block the installer appended to a shell rc (idempotent; leaves the
 *  rest of the file intact). */
function removeInstallerPathLine(rcFile: string): void {
  try {
    const text = readFileOrNull(rcFile);
    if (!text) return;
    const cleaned = text
      .replace(/\n?# added by decompute installer \(PATH\)\nexport PATH="[^"\n]*"\n?/g, "\n")
      .replace(/\n{3,}/g, "\n\n");
    if (cleaned !== text) writeFileAtomic(rcFile, cleaned);
  } catch { /* ignore */ }
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
  // Send the user to Stripe checkout for Pro, tagging the session with THIS device's id so the
  // license minted after payment binds to this machine. After paying, "Activate Pro" fetches it back.
  const base = url || cfg<string>("checkoutUrl", "") || PRO_CHECKOUT_URL;
  const sep = base.includes("?") ? "&" : "?";
  const target = `${base}${sep}client_reference_id=${encodeURIComponent(loadDeviceId())}`;
  await vscode.env.openExternal(vscode.Uri.parse(target));
  // Nudge the user back to activation once they've paid (checkout opens in the browser).
  void vscode.window
    .showInformationMessage(
      "Complete your purchase in the browser, then come back and run “Decompute: Activate Pro”.",
      "Activate Pro",
    )
    .then((pick) => {
      if (pick === "Activate Pro") void activatePro();
    });
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

/** Fetch this device's signed Pro license from the license Worker and install it. Call after the
 *  user has paid via Stripe checkout — the Worker mints the license from the payment webhook, keyed
 *  by the same device id we tagged the checkout with. The gateway then verifies the file offline. */
async function activatePro(): Promise<void> {
  const apiUrl = (cfg<string>("licenseApiUrl", "") || LICENSE_API_URL).replace(/\/+$/, "");
  if (!apiUrl) {
    void vscode.window.showWarningMessage(
      "Pro activation isn't configured yet — the license service URL is unset (decompute.licenseApiUrl).",
    );
    return;
  }
  const device = loadDeviceId();
  try {
    const license = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Activating Decompute Pro…", cancellable: false },
      async () => {
        // The license appears moments after Stripe fires the webhook — poll briefly.
        for (let attempt = 0; attempt < 12; attempt++) {
          try {
            const r = await fetch(`${apiUrl}/license?device=${encodeURIComponent(device)}`);
            if (r.status === 200) {
              const j: any = await r.json();
              if (j?.license) return String(j.license);
            }
          } catch {
            // transient — keep polling
          }
          await new Promise((res) => setTimeout(res, 2500));
        }
        return null;
      },
    );
    if (!license) {
      void vscode.window.showWarningMessage(
        "No Pro license found for this device yet. If you just paid, give it a few seconds and run “Activate Pro” again.",
      );
      return;
    }
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    fs.writeFileSync(LICENSE_FILE, license.trim() + "\n", { mode: 0o600 });
    void vscode.window.showInformationMessage("Decompute Pro is active on this device. 🎉");
    await refresh();
  } catch (e: any) {
    void vscode.window.showErrorMessage("Pro activation failed: " + (e?.message || String(e)));
  }
}

interface StatusAction extends vscode.QuickPickItem {
  id: string;
}

/** The actions menu, as a readable Quick Pick (full labels + descriptions + icons), instead of a
 *  row of message buttons that VS Code truncates to "En…", "Usa…" once there are more than ~3. */
function statusActions(routed: boolean, reachable: boolean, s: any): StatusAction[] {
  const items: StatusAction[] = [];
  if (reachable) {
    items.push(
      routed
        ? { id: "disable", label: "$(circle-slash) Disable routing", description: "Send Claude Code straight to Anthropic" }
        : { id: "enable", label: "$(zap) Enable routing", description: "Route Claude Code through the gateway" }
    );
    items.push({ id: "usage", label: "$(graph) Usage & savings", description: "Tokens saved and cost avoided" });
  }
  items.push({ id: "dashboard", label: "$(dashboard) Open dashboard", description: "Live savings dashboard" });
  items.push({ id: "gateway", label: "$(gear) Set gateway URL", description: gatewayUrl() });
  if (reachable && !isPro(s)) {
    items.push({ id: "upgrade", label: "$(star-full) Upgrade to Pro", description: "Buy Pro — unlimited fair-use compression" });
    items.push({ id: "activatePro", label: "$(check) Activate Pro", description: "Already paid? Fetch your Pro license for this device" });
    items.push({ id: "activate", label: "$(key) Enter license key", description: "Activate with a license key instead" });
  }
  items.push({ id: "setup", label: "$(cloud-download) Run setup", description: "Install or reconnect the Decompute helper" });
  items.push({ id: "copy", label: "$(clippy) Copy setup command", description: "Copy the CLI command for Claude Code" });
  items.push({ id: "local", label: "$(server) Check local gateway", description: "Health-check a separately installed gateway" });
  items.push({ id: "uninstall", label: "$(trash) Uninstall Decompute", description: "Remove the CLI, gateway, config & routing" });
  return items;
}

async function dispatchStatus(id: string | undefined, s: any): Promise<void> {
  switch (id) {
    case "enable": await enableRouting(); break;
    case "disable": await disableRouting(); break;
    case "usage": await showUsagePanel(); break;
    case "copy": await copyClaudeSetupCommand(); break;
    case "dashboard": openDashboard(); break;
    case "gateway": await configureGatewayUrl(); break;
    case "upgrade": await upgrade(); break;
    case "activatePro": await activatePro(); break;
    case "activate": await activateLicense(); break;
    case "setup": await runSetupWizard(); break;
    case "local": await checkLocalGateway(); break;
    case "uninstall": await vscode.commands.executeCommand("decompute.uninstall"); break;
  }
}

async function showStatus(): Promise<void> {
  let s: any;
  let reachable = true;
  try {
    s = await api<any>("/v1/license/status", { method: "GET" });
  } catch {
    reachable = false;
  }
  const routed = claudeCodeRoutedToGateway();
  const title = reachable
    ? `Decompute  ·  Routing ${routed ? "ON" : "OFF"}  ·  ${planSummary(s)}`
    : `Decompute  ·  gateway not reachable at ${gatewayUrl()}`;
  const pick = await vscode.window.showQuickPick(statusActions(routed, reachable, s), {
    title,
    placeHolder: reachable ? gatewayUrl() : "Run setup to install and start the gateway",
  });
  await dispatchStatus(pick?.id, s);
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
