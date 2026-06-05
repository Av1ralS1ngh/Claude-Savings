import * as vscode from "vscode";
import * as os from "os";
import * as fs from "fs";
import * as path from "path";
import { randomUUID } from "crypto";

// Shared local state with the Decompute CLI, so a license activated in the terminal or the
// plugin is the SAME device. We store only a random UUID — never a hardware identifier.
const CONFIG_DIR = path.join(os.homedir(), ".decompute");
const DEVICE_ID_FILE = path.join(CONFIG_DIR, "device.id");
const KEY_FILE = path.join(CONFIG_DIR, "claude.key");
// Claude Code reads an `env` block from ~/.claude/settings.json and applies it to EVERY session —
// both the terminal CLI and the Claude Code VS Code extension panel. Writing the routing env here
// is what makes "Claude Code in VS Code" go through the gateway, not just terminals.
const CLAUDE_SETTINGS = path.join(os.homedir(), ".claude", "settings.json");
const MANAGED_ENV_KEYS = ["ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"] as const;

function cfg<T>(key: string, dflt: T): T {
  return vscode.workspace.getConfiguration("decompute").get<T>(key, dflt);
}
function gatewayUrl(): string {
  return cfg("gatewayUrl", "http://127.0.0.1:8080").replace(/\/+$/, "");
}

function loadDeviceId(): string {
  try {
    const v = fs.readFileSync(DEVICE_ID_FILE, "utf8").trim();
    if (v) return v;
  } catch { /* not created yet */ }
  const id = "dev-" + randomUUID().replace(/-/g, "");
  try {
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    fs.writeFileSync(DEVICE_ID_FILE, id + "\n", { mode: 0o600 });
  } catch { /* best effort */ }
  return id;
}

function newSessionId(): string {
  return "cc-" + randomUUID().replace(/-/g, "").slice(0, 20);
}

async function api(p: string, init: any = {}): Promise<any> {
  const res = await fetch(gatewayUrl() + p, {
    ...init,
    headers: {
      "content-type": "application/json",
      "x-decompute-csrf": "vscode",
      "x-decompute-device": loadDeviceId(),
      ...(init.headers || {}),
    },
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

let statusBar: vscode.StatusBarItem;
let timer: ReturnType<typeof setInterval> | undefined;
let lastPromptAt = 0;

export function activate(context: vscode.ExtensionContext): void {
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.command = "decompute.status";
  context.subscriptions.push(statusBar);

  // SAFE BY DEFAULT: never redirect Claude Code unless the user explicitly opted in. This also
  // self-heals — if a prior version left a routing block, an "off" config cleans it up here.
  wireTerminal(context); // no-ops (clears the collection) when autoWireTerminal is off
  if (cfg("routeClaudeCodePanel", false)) wireClaudeSettings(); else unwireClaudeSettings();

  context.subscriptions.push(
    vscode.commands.registerCommand("decompute.activate", () => activateLicense()),
    vscode.commands.registerCommand("decompute.upgrade", () => upgrade()),
    vscode.commands.registerCommand("decompute.status", () => showStatus()),
    vscode.commands.registerCommand("decompute.setKey", () => setKey(context)),
    vscode.commands.registerCommand("decompute.enableRouting", () => enableRouting(context)),
    vscode.commands.registerCommand("decompute.rewire", () => {
      wireTerminal(context);
      if (cfg("routeClaudeCodePanel", false)) wireClaudeSettings();
      vscode.window.showInformationMessage("Decompute: Claude Code routing re-wired. Reload the Claude Code panel / open a new terminal.");
    }),
    vscode.commands.registerCommand("decompute.disableRouting", async () => {
      const conf = vscode.workspace.getConfiguration("decompute");
      await conf.update("autoWireTerminal", false, vscode.ConfigurationTarget.Global);
      await conf.update("routeClaudeCodePanel", false, vscode.ConfigurationTarget.Global);
      context.environmentVariableCollection.clear();
      unwireClaudeSettings();
      vscode.window.showInformationMessage("Decompute: routing disabled — Claude Code goes directly to Anthropic again.");
    }),
    vscode.commands.registerCommand("decompute.openDashboard", () =>
      vscode.env.openExternal(vscode.Uri.parse(gatewayUrl() + "/dashboard"))
    ),
  );

  maybeWelcome(context);

  // Deep-link activation from the purchase success page or desktop app:
  //   vscode://decompute.decompute/activate?key=DCMP-...
  context.subscriptions.push(
    vscode.window.registerUriHandler({
      handleUri(uri: vscode.Uri) {
        const key = new URLSearchParams(uri.query).get("key");
        if (uri.path.includes("activate") && key) void doActivate(key);
      },
    })
  );

  // React to config changes (gateway url / auto-wire toggle).
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("decompute")) {
        wireTerminal(context);
        void refresh();
      }
    })
  );

  void refresh();
  const poll = Math.max(15, cfg("pollSeconds", 60)) * 1000;
  timer = setInterval(() => void refresh(), poll);
  context.subscriptions.push({ dispose: () => timer && clearInterval(timer) });
}

export function deactivate(): void {
  if (timer) clearInterval(timer);
  // Revert the Claude Code settings env so a disabled/removed extension never leaves Claude Code
  // pointed at a (possibly stopped) gateway. The terminal collection is auto-cleaned by VS Code.
  unwireClaudeSettings();
}

function maybeWelcome(context: vscode.ExtensionContext): void {
  if (context.globalState.get("decompute.welcomed")) return;
  void context.globalState.update("decompute.welcomed", true);
  void vscode.window
    .showInformationMessage(
      "Decompute installed. Claude Code routing is OFF by default. Enable it to compress through the gateway — note: routing needs a funded Anthropic API key (BYOK), not a claude.ai subscription.",
      "Enable Routing",
      "Not now"
    )
    .then((pick) => {
      if (pick === "Enable Routing") void enableRouting(context);
    });
}

async function enableRouting(context: vscode.ExtensionContext): Promise<void> {
  const ok = await vscode.window.showWarningMessage(
    "Route Claude Code through the Decompute gateway?\n\nThis requires a funded Anthropic API key (BYOK). If you use a claude.ai subscription, routing switches billing to the API key and will FAIL if it has no credits.",
    { modal: true },
    "Enable Routing"
  );
  if (ok !== "Enable Routing") return;
  const conf = vscode.workspace.getConfiguration("decompute");
  await conf.update("autoWireTerminal", true, vscode.ConfigurationTarget.Global);
  await conf.update("routeClaudeCodePanel", true, vscode.ConfigurationTarget.Global);
  wireTerminal(context);
  wireClaudeSettings();
  void vscode.window.showInformationMessage(
    "Decompute routing enabled. Reload the Claude Code panel / open a new terminal. Disable anytime via 'Decompute: Disable Routing'."
  );
}

function wireTerminal(context: vscode.ExtensionContext): void {
  const col = context.environmentVariableCollection;
  col.clear();
  if (!cfg("autoWireTerminal", true)) return;
  col.replace("ANTHROPIC_BASE_URL", gatewayUrl());
  col.replace(
    "ANTHROPIC_CUSTOM_HEADERS",
    `x-decompute-session-id: ${newSessionId()}\nx-decompute-device: ${loadDeviceId()}`
  );
  // The BYOK Claude key is intentionally NOT written here. It is read from
  // ~/.decompute/claude.key by the shell at launch, or injected per-session by "Set Claude Key".
  col.description = "Routes Claude Code through the Decompute gateway (base URL + session/device headers).";
}

function readJson(p: string): any {
  try { return JSON.parse(fs.readFileSync(p, "utf8")); } catch { return {}; }
}

// Route the Claude Code VS Code extension (and CLI) through the gateway by adding an `env` block
// to ~/.claude/settings.json. Idempotent + preserves the user's other settings (theme/effort/etc).
function wireClaudeSettings(): void {
  if (!cfg("routeClaudeCodePanel", true)) return;
  const s = readJson(CLAUDE_SETTINGS);
  if (typeof s !== "object" || s === null) return;
  const env = (s.env && typeof s.env === "object") ? s.env : {};
  const headers = `x-decompute-device: ${loadDeviceId()}`;
  if (env.ANTHROPIC_BASE_URL === gatewayUrl() && env.ANTHROPIC_CUSTOM_HEADERS === headers) return; // already wired
  s.env = { ...env, ANTHROPIC_BASE_URL: gatewayUrl(), ANTHROPIC_CUSTOM_HEADERS: headers };
  try {
    fs.mkdirSync(path.dirname(CLAUDE_SETTINGS), { recursive: true });
    fs.writeFileSync(CLAUDE_SETTINGS, JSON.stringify(s, null, 2) + "\n");
  } catch { /* best effort */ }
}

// Remove only the keys we manage, so disabling/uninstalling reverts Claude Code to direct routing
// (otherwise a stale base URL to a stopped gateway would break Claude Code).
function unwireClaudeSettings(): void {
  const s = readJson(CLAUDE_SETTINGS);
  if (!s || typeof s.env !== "object") return;
  for (const k of MANAGED_ENV_KEYS) delete s.env[k];
  if (Object.keys(s.env).length === 0) delete s.env;
  try { fs.writeFileSync(CLAUDE_SETTINGS, JSON.stringify(s, null, 2) + "\n"); } catch { /* best effort */ }
}

async function refresh(): Promise<void> {
  try {
    const s = await api("/v1/license/status", { method: "GET" });
    const pro = s.plan === "pro" || s.license_status === "active";
    if (pro) {
      statusBar.text = "$(zap) Decompute Pro";
      statusBar.tooltip = "Decompute Pro — unlimited fair-use compression on this device.";
    } else {
      const q = s.quota || {};
      statusBar.text = `$(zap) Decompute ${q.remaining ?? "–"}/${q.limit ?? "–"}`;
      statusBar.tooltip = `Free tier: ${q.remaining ?? "?"} of ${q.limit ?? "?"} compressions left in this window. Click for options.`;
    }
    statusBar.show();
    if (s.ui && s.ui.show_modal) void promptUpgrade(s);
  } catch {
    statusBar.text = "$(zap) Decompute";
    statusBar.tooltip = `Gateway not reachable at ${gatewayUrl()}. Start it, or set decompute.gatewayUrl.`;
    statusBar.show();
  }
}

async function promptUpgrade(s: any): Promise<void> {
  const now = Date.now();
  if (now - lastPromptAt < 10 * 60 * 1000) return; // at most once / 10 min so it isn't naggy
  lastPromptAt = now;
  const msg = (s.ui && s.ui.message) ||
    "You reached the free compression limit. Claude keeps working normally; Decompute compression is paused until the reset time.";
  const pick = await vscode.window.showWarningMessage(msg, "Upgrade to Pro", "Activate Key", "Continue without Pro");
  if (pick === "Upgrade to Pro") await upgrade(s.upgrade_url);
  else if (pick === "Activate Key") await activateLicense();
  // "Continue without Pro" just dismisses; compression stays paused until reset.
}

async function upgrade(url?: string): Promise<void> {
  let target = url;
  if (!target) {
    try {
      const s = await api("/v1/license/status", { method: "GET" });
      target = s.upgrade_url;
    } catch { /* fall through to default */ }
  }
  await vscode.env.openExternal(vscode.Uri.parse(target || "https://decompute.ai/pro"));
}

async function activateLicense(): Promise<void> {
  const key = await vscode.window.showInputBox({
    prompt: "Paste your Decompute Pro license key (DCMP-…)",
    ignoreFocusOut: true,
  });
  if (key) await doActivate(key.trim());
}

async function doActivate(key: string): Promise<void> {
  try {
    await api("/v1/license/activate", { method: "POST", body: JSON.stringify({ license_key: key }) });
    vscode.window.showInformationMessage("Decompute Pro activated on this device 🎉");
    await refresh();
  } catch (e: any) {
    vscode.window.showErrorMessage("Activation failed: " + (e?.message || String(e)));
  }
}

async function setKey(context: vscode.ExtensionContext): Promise<void> {
  const key = await vscode.window.showInputBox({
    prompt: "Your Anthropic/Claude key (sk-ant-…). Stored locally at ~/.decompute/claude.key (chmod 600); never synced or committed.",
    password: true,
    ignoreFocusOut: true,
  });
  if (!key) return;
  try {
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    fs.writeFileSync(KEY_FILE, key.trim() + "\n", { mode: 0o600 });
    context.environmentVariableCollection.replace("ANTHROPIC_API_KEY", key.trim());
    vscode.window.showInformationMessage("Claude key saved (chmod 600). Open a new terminal to use it.");
  } catch (e: any) {
    vscode.window.showErrorMessage("Could not save key: " + (e?.message || e));
  }
}

async function showStatus(): Promise<void> {
  try {
    const s = await api("/v1/license/status", { method: "GET" });
    const pro = s.plan === "pro" || s.license_status === "active";
    const q = s.quota || {};
    const line = pro
      ? "Plan: Pro (unlimited fair-use)"
      : `Plan: Free — ${q.remaining ?? "?"}/${q.limit ?? "?"} compressions left this window`;
    const actions = pro ? ["Open Dashboard"] : ["Upgrade to Pro", "Activate Key", "Open Dashboard"];
    const pick = await vscode.window.showInformationMessage(`Decompute · ${line}`, ...actions);
    if (pick === "Upgrade to Pro") await upgrade(s.upgrade_url);
    else if (pick === "Activate Key") await activateLicense();
    else if (pick === "Open Dashboard") await vscode.env.openExternal(vscode.Uri.parse(gatewayUrl() + "/dashboard"));
  } catch {
    vscode.window.showErrorMessage(`Decompute gateway not reachable at ${gatewayUrl()}.`);
  }
}
