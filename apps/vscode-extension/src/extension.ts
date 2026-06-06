import * as vscode from "vscode";
import * as os from "os";
import * as fs from "fs";
import * as path from "path";
import { randomUUID } from "crypto";
import { planWire, planUnwire, readFileOrNull, writeFileAtomic, backupOnce, type Plan } from "./settings";

// Shared local state with the Decompute CLI, so a license activated in the terminal or the
// plugin is the SAME device. We store only a random UUID — never a hardware identifier.
const CONFIG_DIR = path.join(os.homedir(), ".decompute");
const DEVICE_ID_FILE = path.join(CONFIG_DIR, "device.id");
const KEY_FILE = path.join(CONFIG_DIR, "claude.key");
// Claude Code reads an `env` block from ~/.claude/settings.json and applies it to EVERY session —
// both the terminal CLI and the Claude Code VS Code extension panel. Writing the routing env here
// is what makes "Claude Code in VS Code" go through the gateway, not just terminals.
const CLAUDE_SETTINGS = path.join(os.homedir(), ".claude", "settings.json");

function cfg<T>(key: string, dflt: T): T {
  return vscode.workspace.getConfiguration("decompute").get<T>(key, dflt);
}
function gatewayUrl(): string {
  return cfg("gatewayUrl", "http://localhost:8080").replace(/\/+$/, "");
}

// Where "Get Started" points a first-time user who doesn't have the gateway running yet.
// The extension is a CLIENT to a Decompute gateway; without one reachable, Claude Code just goes
// straight to Anthropic (no compression) — so onboarding must explain how to get the gateway up.
const SETUP_URL = "https://decompute.ai";
function openSetup(): void {
  void vscode.env.openExternal(vscode.Uri.parse(SETUP_URL));
}

// Cache the device id for the lifetime of the extension host. If the on-disk write fails
// (e.g. ~/.decompute not writable) we must NOT mint a fresh id on every call — that would make
// the device identity (and thus its license/quota) unstable. Cache the first value we resolve.
let cachedDeviceId: string | undefined;
function loadDeviceId(): string {
  if (cachedDeviceId) return cachedDeviceId;
  try {
    const v = fs.readFileSync(DEVICE_ID_FILE, "utf8").trim();
    if (v) return (cachedDeviceId = v);
  } catch { /* not created yet */ }
  const id = "dev-" + randomUUID().replace(/-/g, "");
  try {
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    fs.writeFileSync(DEVICE_ID_FILE, id + "\n", { mode: 0o600 });
  } catch { /* best effort — still cache below so the id is stable this session */ }
  return (cachedDeviceId = id);
}

let cachedSessionId: string | undefined;
function sessionId(): string {
  // Stable for the lifetime of this window so all terminals share one id (clean support-bundle
  // grouping) instead of minting a fresh id on every wire / config-change / reload.
  return (cachedSessionId ??= "cc-" + randomUUID().replace(/-/g, "").slice(0, 20));
}

async function api(p: string, init: any = {}): Promise<any> {
  // Bound every call: a stalled gateway must not hang refresh() forever or pile up polls.
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 8000);
  try {
    const res = await fetch(gatewayUrl() + p, {
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
      // The gateway should return JSON, but a proxy/HTML error page or empty body must not crash us.
      try { data = JSON.parse(text); } catch { data = { detail: text.slice(0, 300) }; }
    }
    if (!res.ok) throw new Error(data.detail || data?.error?.message || `HTTP ${res.status}`);
    return data;
  } finally {
    clearTimeout(t);
  }
}

async function gatewayHealthy(): Promise<boolean> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 2500);
  try {
    const res = await fetch(gatewayUrl() + "/health", { signal: ctrl.signal });
    return res.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(t); // always clear, even when fetch rejects before the timeout fires
  }
}

/** True if ~/.claude/settings.json currently routes the panel to OUR gateway. */
function panelRoutedToGateway(): boolean {
  let text: string | null;
  try { text = readFileOrNull(CLAUDE_SETTINGS); } catch { return false; }
  if (!text) return false;
  try {
    const obj = JSON.parse(text);
    return !!(obj && typeof obj === "object" && obj.env && obj.env.ANTHROPIC_BASE_URL === gatewayUrl());
  } catch {
    return false;
  }
}

let statusBar: vscode.StatusBarItem;
let timer: ReturnType<typeof setInterval> | undefined;
let lastPromptAt = 0;

function pollMs(): number {
  // Coerce any on-disk value: a non-numeric decompute.pollSeconds must not become setInterval(NaN)
  // (which fires immediately, forever — a network busy-loop).
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

  // Don't let VS Code re-apply a cached routing env to restored terminals before we re-sync —
  // otherwise "routing off" can silently come back on a restart. We rebuild the collection from
  // the current settings on every activate instead.
  context.environmentVariableCollection.persistent = false;

  // Self-heal on activate: make on-disk routing match the user's setting (safe by default — off).
  syncRouting(context);

  context.subscriptions.push(
    vscode.commands.registerCommand("decompute.activate", () => activateLicense()),
    vscode.commands.registerCommand("decompute.upgrade", () => upgrade()),
    vscode.commands.registerCommand("decompute.status", () => showStatus()),
    vscode.commands.registerCommand("decompute.setKey", () => setKey()),
    vscode.commands.registerCommand("decompute.enableRouting", () => enableRouting(context)),
    vscode.commands.registerCommand("decompute.rewire", () => {
      syncRouting(context);
      void refresh();
      vscode.window.showInformationMessage(
        "Decompute: routing re-applied. Reload the Claude Code panel and open new terminals for it to take effect."
      );
    }),
    vscode.commands.registerCommand("decompute.disableRouting", () => disableRouting(context)),
    vscode.commands.registerCommand("decompute.openDashboard", () =>
      vscode.env.openExternal(vscode.Uri.parse(gatewayUrl() + "/dashboard"))
    ),
    vscode.commands.registerCommand("decompute.getStarted", () => openSetup()),
  );

  maybeWelcome(context);

  // Deep-link activation from the purchase success page or desktop app:
  //   vscode://decompute.decompute/activate?key=DCMP-...
  context.subscriptions.push(
    vscode.window.registerUriHandler({
      handleUri(uri: vscode.Uri) {
        if (uri.path.replace(/^\/+/, "") !== "activate") return; // exact path, not substring
        const key = new URLSearchParams(uri.query).get("key");
        // Only act on something shaped like a real license key — never feed arbitrary query input
        // straight into an activation call.
        if (key && /^DCMP-[A-Za-z0-9_-]{8,}$/.test(key)) void doActivate(key);
      },
    })
  );

  // React to config changes (gateway url / routing toggles): keep both switches consistent.
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("decompute")) {
        syncRouting(context);
        void refresh();
        if (e.affectsConfiguration("decompute.pollSeconds")) restartPolling();
      }
    })
  );

  void refresh();
  restartPolling();
  context.subscriptions.push({ dispose: () => timer && clearInterval(timer) });
}

export function deactivate(): void {
  if (timer) clearInterval(timer);
  // NOTE: we intentionally do NOT unwire ~/.claude/settings.json here. deactivate() fires on every
  // window reload; unwiring would silently turn the user's routing OFF on each reload (the
  // "it keeps disabling itself" bug). Routing is removed only on an explicit Disable Routing.
}

/** Make the terminal env-collection and ~/.claude/settings.json match the current settings. */
function syncRouting(context: vscode.ExtensionContext): void {
  wireTerminal(context); // clears the collection when autoWireTerminal is off
  if (cfg("routeClaudeCodePanel", false)) wireClaudeSettings(); else unwireClaudeSettings();
}

// Claude Code captures ANTHROPIC_BASE_URL at session start and doesn't re-read it mid-session, so an
// already-running panel/terminal keeps its old routing until the window reloads. We can't re-route a
// live process from here — so offer a one-click reload to apply the change immediately.
async function offerReload(message: string): Promise<void> {
  const pick = await vscode.window.showWarningMessage(message, "Reload Window", "Later");
  if (pick === "Reload Window") await vscode.commands.executeCommand("workbench.action.reloadWindow");
}

function maybeWelcome(context: vscode.ExtensionContext): void {
  if (context.globalState.get("decompute.welcomed")) return;
  void context.globalState.update("decompute.welcomed", true);
  void vscode.window
    .showInformationMessage(
      "Decompute installed. It compresses Claude Code's context through a local Decompute gateway. " +
        "You need the gateway running + a funded Anthropic key (BYOK) — routing stays OFF until you enable it. New here? Start with Get Started.",
      "Get Started",
      "Enable Routing",
      "Not now"
    )
    .then((pick) => {
      if (pick === "Get Started") openSetup();
      else if (pick === "Enable Routing") void enableRouting(context);
    });
}

async function enableRouting(context: vscode.ExtensionContext): Promise<void> {
  const ok = await vscode.window.showWarningMessage(
    "Route Claude Code through the Decompute gateway?\n\nThis requires a funded Anthropic API key (BYOK). If you use a claude.ai subscription, routing switches billing to the API key and will FAIL if it has no credits.",
    { modal: true },
    "Enable Routing"
  );
  if (ok !== "Enable Routing") return;

  // Don't silently break Claude Code by routing to a gateway that isn't up.
  if (!(await gatewayHealthy())) {
    const cont = await vscode.window.showWarningMessage(
      `The Decompute gateway isn't reachable at ${gatewayUrl()}. If you enable routing now, Claude Code will fail until the gateway is running. Enable anyway?`,
      "Enable anyway",
      "Cancel"
    );
    if (cont !== "Enable anyway") return;
  }

  const conf = vscode.workspace.getConfiguration("decompute");
  await conf.update("autoWireTerminal", true, vscode.ConfigurationTarget.Global);
  await conf.update("routeClaudeCodePanel", true, vscode.ConfigurationTarget.Global);
  wireTerminal(context);
  const r = wireClaudeSettings();
  void refresh();
  if (r.status === "unparseable") return; // wireClaudeSettings already warned
  await offerReload(
    "Decompute routing enabled. An already-open Claude Code panel won't route until the window reloads — reload now to apply?"
  );
}

async function disableRouting(context: vscode.ExtensionContext): Promise<void> {
  const conf = vscode.workspace.getConfiguration("decompute");
  await conf.update("autoWireTerminal", false, vscode.ConfigurationTarget.Global);
  await conf.update("routeClaudeCodePanel", false, vscode.ConfigurationTarget.Global);
  wireTerminal(context); // autoWireTerminal is now off → deletes only the routing keys (keeps any other env)
  unwireClaudeSettings();
  void refresh();
  // Don't claim success if the settings.json removal didn't actually take (unwritable / not JSON).
  if (panelRoutedToGateway()) {
    void vscode.window.showWarningMessage(
      `Decompute: couldn't remove the routing block from ~/.claude/settings.json. Open it and delete the "env" ANTHROPIC_BASE_URL/ANTHROPIC_CUSTOM_HEADERS entries manually.`
    );
    return;
  }
  // Env is captured at process start, so "off" only applies to NEW sessions until a reload.
  await offerReload(
    "Decompute routing disabled. Already-running Claude Code sessions and terminals keep hitting the gateway until the window reloads — reload now to fully stop that traffic?"
  );
}

function wireTerminal(context: vscode.ExtensionContext): void {
  const col = context.environmentVariableCollection;
  // Delete ONLY the routing keys we manage — never col.clear(), which would also wipe an
  // ANTHROPIC_API_KEY some other tooling may have set in this collection.
  col.delete("ANTHROPIC_BASE_URL");
  col.delete("ANTHROPIC_CUSTOM_HEADERS");
  if (!cfg("autoWireTerminal", false)) return;
  col.replace("ANTHROPIC_BASE_URL", gatewayUrl());
  col.replace(
    "ANTHROPIC_CUSTOM_HEADERS",
    `x-decompute-session-id: ${sessionId()}\nx-decompute-device: ${loadDeviceId()}`
  );
  // The BYOK Claude key is intentionally NOT written here (it must not live in VS Code's synced
  // state). It is read from ~/.decompute/claude.key by the shell at launch.
  col.description = "Routes Claude Code through the Decompute gateway (base URL + session/device headers).";
}

// Route the Claude Code panel (and CLI) through the gateway via an `env` block in
// ~/.claude/settings.json. Preserves the user's other settings; never clobbers an unparseable file.
function wireClaudeSettings(): Plan {
  if (!cfg("routeClaudeCodePanel", false)) return { text: null, status: "nochange" };
  let text: string | null;
  try {
    text = readFileOrNull(CLAUDE_SETTINGS);
  } catch (e) {
    vscode.window.showWarningMessage(`Decompute: couldn't read ~/.claude/settings.json (${e}); routing not applied.`);
    return { text: null, status: "unparseable" };
  }
  const headers = `x-decompute-device: ${loadDeviceId()}`;
  const plan = planWire(text, gatewayUrl(), headers);
  if (plan.status === "unparseable") {
    void vscode.window.showWarningMessage(
      "Decompute: ~/.claude/settings.json isn't valid JSON, so routing was NOT applied (your file was left untouched). Fix the file, then run 'Decompute: Re-wire Claude Code Routing'."
    );
    return plan;
  }
  if (plan.text === null) return plan; // already wired
  try {
    backupOnce(CLAUDE_SETTINGS, text);
    writeFileAtomic(CLAUDE_SETTINGS, plan.text);
  } catch (e) {
    void vscode.window.showWarningMessage(`Decompute: failed to update ~/.claude/settings.json: ${e}`);
  }
  return plan;
}

// Remove only the keys we manage; preserve everything else; never touch an unparseable file.
function unwireClaudeSettings(): Plan {
  let text: string | null;
  try {
    text = readFileOrNull(CLAUDE_SETTINGS);
  } catch {
    return { text: null, status: "nochange" };
  }
  const plan = planUnwire(text);
  if (plan.text === null) return plan; // nothing to remove, or unparseable (leave it alone)
  try {
    writeFileAtomic(CLAUDE_SETTINGS, plan.text);
  } catch {
    /* best effort */
  }
  return plan;
}

async function refresh(): Promise<void> {
  const routed = panelRoutedToGateway() || cfg("autoWireTerminal", false);
  try {
    const s = await api("/v1/license/status", { method: "GET" });
    const pro = s.plan === "pro" || s.license_status === "active";
    const q = s.quota || {};
    if (!routed) {
      statusBar.text = "$(circle-slash) Decompute off";
      statusBar.tooltip = "Routing OFF — Claude Code goes directly to Anthropic (no compression). Click for options / enable routing.";
    } else if (pro) {
      statusBar.text = "$(zap) Decompute Pro";
      statusBar.tooltip = "Routing ON · Pro — unlimited fair-use compression on this device.";
    } else if (s.license_status === "paused" || s.compression_allowed === false) {
      statusBar.text = "$(debug-pause) Decompute Paused";
      statusBar.tooltip = "Routing ON · Free quota used up — compression is paused until reset. Claude still works (uncompressed). Click to upgrade or activate Pro.";
    } else {
      statusBar.text = `$(zap) Decompute Free ${q.remaining ?? "–"}/${q.limit ?? "–"}`;
      statusBar.tooltip = `Routing ON · Free tier: ${q.remaining ?? "?"} of ${q.limit ?? "?"} compressions left this window. Click for options.`;
    }
    statusBar.show();
    if (routed && s.ui && s.ui.show_modal) void promptUpgrade(s);
  } catch {
    statusBar.text = routed ? "$(warning) Decompute" : "$(circle-slash) Decompute off";
    statusBar.tooltip = routed
      ? `Routing is ON but the gateway is NOT reachable at ${gatewayUrl()} — Claude Code will fail until it's running (or disable routing).`
      : `Decompute gateway not reachable at ${gatewayUrl()}. Routing is off, so Claude Code is unaffected.`;
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
  // "Continue without Pro" just dismisses; compression stays paused until reset (Claude still works).
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

async function setKey(): Promise<void> {
  const key = await vscode.window.showInputBox({
    prompt: "Your Anthropic API key. Stored locally at ~/.decompute/claude.key (owner-only, chmod 600); never synced or committed.",
    password: true,
    ignoreFocusOut: true,
  });
  if (!key) return;
  try {
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    try { fs.chmodSync(CONFIG_DIR, 0o700); } catch { /* best effort */ }
    fs.writeFileSync(KEY_FILE, key.trim() + "\n", { mode: 0o600 });
    // writeFileSync's `mode` is ignored when the file already exists — enforce 0600 explicitly so a
    // pre-existing looser-perm key file gets tightened.
    try { fs.chmodSync(KEY_FILE, 0o600); } catch { /* best effort */ }
    // The key is intentionally NOT written into VS Code's environmentVariableCollection: that store
    // is persistent and Settings-Sync-eligible, which would break the "local, chmod-600, never synced"
    // guarantee. The CLI/shell reads ~/.decompute/claude.key for BYOK terminal sessions; a self-hosted
    // gateway uses its own ANTHROPIC_API_KEY for upstream and ignores a client-supplied key.
    vscode.window.showInformationMessage(
      "Claude key saved to ~/.decompute/claude.key (chmod 600; never synced)."
    );
  } catch (e: any) {
    vscode.window.showErrorMessage("Could not save key: " + (e?.message || e));
  }
}

async function showStatus(): Promise<void> {
  const routed = panelRoutedToGateway() || cfg("autoWireTerminal", false);
  try {
    const s = await api("/v1/license/status", { method: "GET" });
    const pro = s.plan === "pro" || s.license_status === "active";
    const q = s.quota || {};
    const routeLine = routed ? "Routing: ON (→ gateway)" : "Routing: OFF (direct to Anthropic)";
    const paused = !pro && (s.license_status === "paused" || s.compression_allowed === false);
    const planLine = pro
      ? "Plan: Pro (unlimited fair-use)"
      : paused
        ? `Plan: Free — PAUSED (quota used up; resets next window). Claude still works, uncompressed.`
        : `Plan: Free — ${q.remaining ?? "?"}/${q.limit ?? "?"} compressions left this window`;
    const actions = [
      routed ? "Disable Routing" : "Enable Routing",
      ...(pro ? [] : ["Upgrade to Pro", "Activate Key"]),
      "Open Dashboard",
    ];
    const pick = await vscode.window.showInformationMessage(`Decompute · ${routeLine} · ${planLine}`, ...actions);
    if (pick === "Enable Routing") await vscode.commands.executeCommand("decompute.enableRouting");
    else if (pick === "Disable Routing") await vscode.commands.executeCommand("decompute.disableRouting");
    else if (pick === "Upgrade to Pro") await upgrade(s.upgrade_url);
    else if (pick === "Activate Key") await activateLicense();
    else if (pick === "Open Dashboard") await vscode.env.openExternal(vscode.Uri.parse(gatewayUrl() + "/dashboard"));
  } catch {
    // First-time / no-gateway path: don't dead-end on an error — guide the user to set it up.
    const pick = await vscode.window.showWarningMessage(
      `Decompute gateway not reachable at ${gatewayUrl()}. Start the Decompute gateway, then it works. ` +
        `(Routing is currently ${routed ? "ON" : "OFF"}.)`,
      "Get Started",
      "Set Gateway URL",
      routed ? "Disable Routing" : "Enable Routing"
    );
    if (pick === "Get Started") openSetup();
    else if (pick === "Set Gateway URL") await vscode.commands.executeCommand("workbench.action.openSettings", "decompute.gatewayUrl");
    else if (pick === "Enable Routing") await vscode.commands.executeCommand("decompute.enableRouting");
    else if (pick === "Disable Routing") await vscode.commands.executeCommand("decompute.disableRouting");
  }
}
