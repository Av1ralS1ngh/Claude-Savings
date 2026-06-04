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

  wireTerminal(context);

  context.subscriptions.push(
    vscode.commands.registerCommand("decompute.activate", () => activateLicense()),
    vscode.commands.registerCommand("decompute.upgrade", () => upgrade()),
    vscode.commands.registerCommand("decompute.status", () => showStatus()),
    vscode.commands.registerCommand("decompute.setKey", () => setKey(context)),
    vscode.commands.registerCommand("decompute.rewire", () => {
      wireTerminal(context);
      vscode.window.showInformationMessage("Decompute: terminal routing re-wired. Open a new terminal.");
    }),
    vscode.commands.registerCommand("decompute.openDashboard", () =>
      vscode.env.openExternal(vscode.Uri.parse(gatewayUrl() + "/dashboard"))
    ),
  );

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
