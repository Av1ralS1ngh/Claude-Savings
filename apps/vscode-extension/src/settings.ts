// Pure, dependency-free planning + safe I/O for ~/.claude/settings.json.
//
// No `vscode` import, so the risky bits (deciding what to write to the user's Claude Code config,
// and never clobbering it) are unit-testable in plain Node. The extension layer handles the UI.

import * as fs from "fs";
import * as path from "path";

export const MANAGED_ENV_KEYS = ["ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"] as const;

export type PlanStatus =
  | "created" // file didn't exist; a new one with just our env block is proposed
  | "wired" // merged our env into the user's existing settings
  | "already" // already wired with the same values; nothing to do
  | "removed" // our managed keys were stripped out
  | "nochange" // nothing of ours to remove
  | "unparseable"; // file exists but isn't valid JSON — DO NOT touch it

export interface Plan {
  /** New file contents to write, or null when nothing should be written. */
  text: string | null;
  status: PlanStatus;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/**
 * Decide how to wire routing into settings.json.
 * @param existingText current file contents, or null if the file does not exist.
 *
 * Safety: if the file exists but is not valid JSON (comments / trailing commas / corrupt), we
 * return {text:null, status:"unparseable"} and the caller must NOT overwrite it — otherwise the
 * user loses their whole config.
 */
export function planWire(existingText: string | null, baseUrl: string, headers: string): Plan {
  if (existingText === null) {
    const obj = { env: { ANTHROPIC_BASE_URL: baseUrl, ANTHROPIC_CUSTOM_HEADERS: headers } };
    return { text: JSON.stringify(obj, null, 2) + "\n", status: "created" };
  }
  let obj: unknown;
  try {
    obj = JSON.parse(existingText);
  } catch {
    return { text: null, status: "unparseable" };
  }
  if (!isPlainObject(obj)) return { text: null, status: "unparseable" };
  // env present but not a plain object (array/string/null) -> hand-managed; don't clobber.
  if (obj.env !== undefined && !isPlainObject(obj.env)) return { text: null, status: "unparseable" };

  const env = isPlainObject(obj.env) ? (obj.env as Record<string, unknown>) : {};
  if (env.ANTHROPIC_BASE_URL === baseUrl && env.ANTHROPIC_CUSTOM_HEADERS === headers) {
    return { text: null, status: "already" };
  }
  obj.env = { ...env, ANTHROPIC_BASE_URL: baseUrl, ANTHROPIC_CUSTOM_HEADERS: headers };
  return { text: JSON.stringify(obj, null, 2) + "\n", status: "wired" };
}

/** Remove ONLY our managed keys, preserving everything else; never touch an unparseable file. */
export function planUnwire(existingText: string | null): Plan {
  if (existingText === null) return { text: null, status: "nochange" };
  let obj: unknown;
  try {
    obj = JSON.parse(existingText);
  } catch {
    return { text: null, status: "unparseable" };
  }
  if (!isPlainObject(obj) || !isPlainObject(obj.env)) return { text: null, status: "nochange" };

  const env = obj.env as Record<string, unknown>;
  let changed = false;
  for (const k of MANAGED_ENV_KEYS) {
    if (k in env) {
      delete env[k];
      changed = true;
    }
  }
  if (Object.keys(env).length === 0) delete obj.env;
  if (!changed) return { text: null, status: "nochange" };
  return { text: JSON.stringify(obj, null, 2) + "\n", status: "removed" };
}

/** Read a file, returning null ONLY when it genuinely doesn't exist; rethrow other errors so the
 * caller can abort instead of treating it as "missing -> create". */
export function readFileOrNull(file: string): string | null {
  try {
    return fs.readFileSync(file, "utf8");
  } catch (e) {
    if ((e as NodeJS.ErrnoException)?.code === "ENOENT") return null;
    throw e;
  }
}

/** Atomic write that follows symlinks (dotfiles-safe) and preserves the file's existing mode. */
export function writeFileAtomic(file: string, text: string): void {
  let target = file;
  try {
    target = fs.realpathSync(file);
  } catch (e) {
    if ((e as NodeJS.ErrnoException)?.code !== "ENOENT") throw e;
  }
  const dir = path.dirname(target);
  fs.mkdirSync(dir, { recursive: true });
  let mode = 0o600;
  try {
    mode = fs.statSync(target).mode & 0o777;
  } catch {
    /* new file -> default 0o600 */
  }
  const tmp = path.join(dir, `.${path.basename(target)}.decompute.${process.pid}.tmp`);
  fs.writeFileSync(tmp, text, { mode });
  try {
    fs.renameSync(tmp, target);
  } catch (e) {
    try {
      fs.unlinkSync(tmp);
    } catch {
      /* ignore */
    }
    throw e;
  }
}

/** One-time backup of the user's original settings.json before we ever modify it. */
export function backupOnce(file: string, currentText: string | null): void {
  if (currentText === null) return;
  const bak = file + ".decompute-backup";
  try {
    if (!fs.existsSync(bak)) fs.writeFileSync(bak, currentText, { mode: 0o600 });
  } catch {
    /* best effort */
  }
}
