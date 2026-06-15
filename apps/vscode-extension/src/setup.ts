import * as os from "os";
import * as path from "path";

// Pure, testable onboarding helpers. All filesystem / VS Code I/O lives in extension.ts; this module
// only computes paths, quoting, the install command, and which onboarding step is next — so the
// branching logic can be unit-tested without a VS Code host.

type Env = Record<string, string | undefined>;

/** The CLI's config dir — must match config.py: $DECOMPUTE_CONFIG_DIR or ~/.decompute. */
export function configDir(home: string = os.homedir(), env: Env = process.env): string {
  return env.DECOMPUTE_CONFIG_DIR || path.join(home, ".decompute");
}

/** Absolute paths the `decompute` CLI shim is installed to, honoring the installer's env overrides
 *  (install.sh: DECOMPUTE_BIN for the shim dir, DECOMPUTE_HOME for the venv). Defaults:
 *  - shim on PATH at ~/.local/bin/decompute
 *  - venv at ~/.decompute/venv/bin/decompute (fallback if the shim was skipped) */
export function cliBinCandidates(home: string = os.homedir(), env: Env = process.env): string[] {
  const out: string[] = [];
  if (env.DECOMPUTE_BIN) out.push(path.join(env.DECOMPUTE_BIN, "decompute"));
  out.push(path.join(home, ".local", "bin", "decompute"));
  const installHome = env.DECOMPUTE_HOME || path.join(home, ".decompute");
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

/** Canonical one-line installer (isolated venv + PATH shim). Overridable via the
 *  `decompute.cliInstallCommand` setting (e.g. a local checkout: `bash /path/to/repo/scripts/install.sh`). */
export const DEFAULT_INSTALL_COMMAND =
  "curl -fsSL https://pub-1093626231534fd593c5355928f4074d.r2.dev/install.sh | bash";

/** The install command to run in a terminal: the user's override if set, else the default. */
export function resolveInstallCommand(setting: string | null | undefined): string {
  const s = String(setting ?? "").trim();
  return s || DEFAULT_INSTALL_COMMAND;
}

/** POSIX shell-quote a value for safe interpolation into a terminal command (paths, URLs, keys). */
export function shQuote(value: string): string {
  if (value === "") return "''";
  if (/^[A-Za-z0-9_./:=+-]+$/.test(value)) return value;
  return "'" + value.replace(/'/g, "'\\''") + "'";
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
