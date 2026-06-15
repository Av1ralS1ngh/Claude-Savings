import * as os from "os";
import * as path from "path";

// Pure, testable onboarding helpers. All filesystem / VS Code I/O lives in extension.ts; this module
// only computes paths, quoting, the install command, and which onboarding step is next — so the
// branching logic can be unit-tested without a VS Code host.

type Env = Record<string, string | undefined>;

/** OS family the extension targets. We branch CLI/venv/shell behavior on this — kept as a parameter
 *  (defaulting to process.platform) so the pure helpers below are unit-testable for every OS on a Mac. */
export type Platform = "win32" | "darwin" | "linux";

/** The CLI's config dir — must match config.py: $DECOMPUTE_CONFIG_DIR or ~/.decompute. Cross-OS:
 *  Path.home()/os.homedir() resolves to C:\\Users\\<user> on Windows, so ~/.decompute works there too
 *  (we deliberately do NOT use %APPDATA% — one config location across OSes, see config.py). */
export function configDir(home: string = os.homedir(), env: Env = process.env): string {
  return env.DECOMPUTE_CONFIG_DIR || path.join(home, ".decompute");
}

/** Absolute paths the `decompute` CLI is installed to, honoring the installer's env overrides
 *  (DECOMPUTE_BIN for the shim/Scripts dir, DECOMPUTE_HOME for the venv). The venv layout differs by OS:
 *  POSIX uses `venv/bin/decompute`; Windows uses `venv\\Scripts\\decompute.exe` (pip's entry-point .exe).
 *  Defaults:
 *  - POSIX:   shim at ~/.local/bin/decompute, else venv at ~/.decompute/venv/bin/decompute
 *  - Windows: ~/.decompute/venv/Scripts/decompute.exe (no separate shim dir — the venv Scripts dir
 *             is put on PATH directly; pip generates decompute.exe there). */
export function cliBinCandidates(home: string = os.homedir(), env: Env = process.env, platform?: Platform): string[] {
  const isWin = (platform || (process.platform as Platform)) === "win32";
  const installHome = env.DECOMPUTE_HOME || path.join(home, ".decompute");
  if (isWin) {
    const out: string[] = [];
    // DECOMPUTE_BIN, when set, is the dir the .exe entry point lives in (the venv Scripts dir).
    if (env.DECOMPUTE_BIN) out.push(path.join(env.DECOMPUTE_BIN, "decompute.exe"));
    out.push(path.join(installHome, "venv", "Scripts", "decompute.exe"));
    return out;
  }
  const out: string[] = [];
  if (env.DECOMPUTE_BIN) out.push(path.join(env.DECOMPUTE_BIN, "decompute"));
  out.push(path.join(home, ".local", "bin", "decompute"));
  out.push(path.join(installHome, "venv", "bin", "decompute"));
  return out;
}

/** Files `decompute init` writes — presence means the CLI has been set up at least once.
 *  init writes config.toml; we also tolerate config.json from older/alternate layouts. */
export function configFiles(home: string = os.homedir(), env: Env = process.env): string[] {
  const dir = configDir(home, env);
  return [path.join(dir, "config.toml"), path.join(dir, "config.json")];
}

/** The BYOK Claude key `decompute init` writes (chmod 600). Its presence means a key is connected. */
export function claudeKeyFile(home: string = os.homedir(), env: Env = process.env): string {
  return path.join(configDir(home, env), "claude.key");
}

const DIST_BASE = "https://pub-1093626231534fd593c5355928f4074d.r2.dev";

/** Canonical one-line installer (isolated venv + PATH shim). Overridable via the
 *  `decompute.cliInstallCommand` setting (e.g. a local checkout: `bash /path/to/repo/scripts/install.sh`). */
export const DEFAULT_INSTALL_COMMAND =
  `curl -fsSL ${DIST_BASE}/install.sh | bash`;

/** Windows installer: a PowerShell one-liner fetching install.ps1 (the native-Windows equivalent of
 *  install.sh — venv under Scripts\\, PATH via setx, auto-start via Task Scheduler). Runs in a
 *  PowerShell terminal the extension opens. */
export const DEFAULT_INSTALL_COMMAND_WINDOWS =
  `powershell -ExecutionPolicy Bypass -NoProfile -Command "irm ${DIST_BASE}/install.ps1 | iex"`;

/** The install command to run in a terminal: the user's override if set, else the per-OS default. */
export function resolveInstallCommand(setting: string | null | undefined, platform?: Platform): string {
  const s = String(setting ?? "").trim();
  if (s) return s; // an explicit user/machine override always wins, on every OS
  const isWin = (platform || (process.platform as Platform)) === "win32";
  return isWin ? DEFAULT_INSTALL_COMMAND_WINDOWS : DEFAULT_INSTALL_COMMAND;
}

/** POSIX shell-quote a value for safe interpolation into a terminal command (paths, URLs, keys). */
export function shQuote(value: string): string {
  if (value === "") return "''";
  if (/^[A-Za-z0-9_./:=+-]+$/.test(value)) return value;
  return "'" + value.replace(/'/g, "'\\''") + "'";
}

/** PowerShell-quote a value for safe interpolation into a command sent to a Windows PowerShell terminal.
 *  PowerShell single-quoted strings are literal; the only escape is a doubled single-quote (`''`). This
 *  neutralizes spaces, `;`, `&`, `|`, backticks, and `$` (none are special inside single quotes). */
export function shQuoteWin(value: string): string {
  if (value === "") return "''";
  // Safe bare tokens (paths/URLs/flags) pass through for readability; everything else is single-quoted.
  if (/^[A-Za-z0-9_.:/\\=+-]+$/.test(value)) return value;
  return "'" + value.replace(/'/g, "''") + "'";
}

/** Quote a value for the terminal the extension will run the command in, by OS. */
export function shQuoteFor(value: string, platform?: Platform): string {
  const isWin = (platform || (process.platform as Platform)) === "win32";
  return isWin ? shQuoteWin(value) : shQuote(value);
}

// ── OS / environment detection ────────────────────────────────────────────────
// The extension must tell three setups apart, because each has a DIFFERENT install path:
//   - windows  : native Windows (process.platform === 'win32') -> PowerShell install.ps1
//   - wsl      : VS Code's extension host runs in WSL (Linux), but the machine is Windows. The bash
//                install.sh works here; we just flag it so the UI is honest about where it installed.
//   - macos / linux : native Unix -> curl|bash install.sh.
export type OSKind = "windows" | "wsl" | "macos" | "linux";

export interface EnvInfo {
  kind: OSKind;
  /** True when running over WSL (extension host is Linux but the host machine is Windows). */
  isWsl: boolean;
  /** A short human label for the status UI, e.g. "Windows", "Windows (WSL)", "macOS", "Linux". */
  label: string;
  /** The platform whose conventions to use for install command / quoting / venv layout. WSL is POSIX. */
  platform: Platform;
}

export interface DetectInputs {
  platform?: NodeJS.Platform | Platform;
  /** os.release() — under WSL this contains "microsoft" / "WSL" (e.g. "5.15.0-microsoft-standard-WSL2"). */
  osRelease?: string;
  /** vscode.env.remoteName — "wsl" (or "wsl-<distro>") when connected to a WSL distro. */
  remoteName?: string;
}

/** Detect the OS/environment from injectable inputs (so it's unit-testable on any machine). The extension
 *  passes os.release() and vscode.env.remoteName; everything else defaults to the live process. */
export function detectEnv(inputs: DetectInputs = {}): EnvInfo {
  const platform = (inputs.platform || process.platform) as Platform;
  if (platform === "win32") {
    return { kind: "windows", isWsl: false, label: "Windows", platform: "win32" };
  }
  // Linux extension host: could be a real Linux box, or VS Code attached to WSL on a Windows machine.
  const remote = (inputs.remoteName || "").toLowerCase();
  const release = (inputs.osRelease || "").toLowerCase();
  const isWsl = platform === "linux" && (remote.startsWith("wsl") || release.includes("microsoft") || release.includes("wsl"));
  if (isWsl) {
    return { kind: "wsl", isWsl: true, label: "Windows (WSL)", platform: "linux" };
  }
  if (platform === "darwin") {
    return { kind: "macos", isWsl: false, label: "macOS", platform: "darwin" };
  }
  return { kind: "linux", isWsl: false, label: "Linux", platform: "linux" };
}

export interface SetupState {
  installed: boolean;
  configured: boolean;
  keyPresent: boolean;
  routed: boolean;
  authMode?: "anthropic_api" | "claude_subscription" | "enterprise_gateway";
}

export type SetupStep = "install" | "connect-key" | "route" | "verify" | "done";

/** The first onboarding step that still needs doing, given the detected state. The wizard starts
 *  here and runs through to verify; "done" means everything is already in place. */
export function nextSetupStep(s: SetupState): SetupStep {
  if (!s.installed) return "install";
  if (!s.configured || (s.authMode !== "claude_subscription" && !s.keyPresent)) return "connect-key";
  if (!s.routed) return "route";
  return "done";
}

/** A short, human label for a step (used in messages / tests). */
export function stepLabel(step: SetupStep): string {
  switch (step) {
    case "install": return "Install the Decompute CLI";
    case "connect-key": return "Connect your Claude key";
    case "route": return "Route Claude Code through the gateway";
    case "verify": return "Verify the connection";
    case "done": return "Decompute is set up";
  }
}
