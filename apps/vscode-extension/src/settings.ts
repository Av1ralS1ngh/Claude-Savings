// Pure, dependency-free planning + safe I/O for ~/.claude/settings.json.
//
// This module deliberately does NOT import `vscode`, so the risky bits (deciding what to
// write to the user's Claude Code config, and never clobbering it) are unit-testable in
// plain Node. The extension layer handles UI/notifications around these results.

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
  /** The new file contents to write, or null when nothing should be written. */
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
 * Critical safety property: if the file exists but is not valid JSON (e.g. it contains
 * comments / trailing commas / is corrupt), we return {text:null, status:"unparseable"}
 * and the caller must NOT overwrite it — otherwise the user loses their whole config.
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

  // If `env` exists but isn't a plain object (array/string/number/null), treat the file as
  // hand-managed and refuse to touch it — overwriting would silently destroy the user's value.
  if (obj.env !== undefined && !isPlainObject(obj.env)) return { text: null, status: "unparseable" };
  const env = isPlainObject(obj.env) ? (obj.env as Record<string, unknown>) : {};
  if (env.ANTHROPIC_BASE_URL === baseUrl && env.ANTHROPIC_CUSTOM_HEADERS === headers) {
    return { text: null, status: "already" };
  }
  obj.env = { ...env, ANTHROPIC_BASE_URL: baseUrl, ANTHROPIC_CUSTOM_HEADERS: headers };
  return { text: JSON.stringify(obj, null, 2) + "\n", status: "wired" };
}

/**
 * Decide how to remove ONLY our managed keys from settings.json, preserving everything else.
 * Never touches an unparseable file.
 */
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

/** Read a file, returning null ONLY when it genuinely doesn't exist; rethrow other errors
 * (e.g. permission) so the caller can abort instead of treating it as "missing → create". */
export function readFileOrNull(file: string): string | null {
  try {
    return fs.readFileSync(file, "utf8");
  } catch (e) {
    if ((e as NodeJS.ErrnoException)?.code === "ENOENT") return null;
    throw e;
  }
}

/** Atomic write: write a sibling temp file, then rename over the target, so a crash mid-write
 * can never leave the user's Claude config half-written/corrupt.
 *
 * If `file` is a symlink (common when ~/.claude/settings.json is managed in a dotfiles repo),
 * we resolve it and write THROUGH the link — updating the real backing file and preserving the
 * symlink — instead of replacing the link with a regular file. We also preserve the target's
 * existing permissions, only defaulting to 0o600 when creating a brand-new file. */
export function writeFileAtomic(file: string, text: string): void {
  let target = file;
  try {
    target = fs.realpathSync(file); // follow a symlink to its real backing file
  } catch (e) {
    // ENOENT = the file/symlink doesn't exist yet → create at the original path.
    if ((e as NodeJS.ErrnoException)?.code !== "ENOENT") throw e;
  }
  const dir = path.dirname(target);
  fs.mkdirSync(dir, { recursive: true });
  let mode = 0o600;
  try {
    mode = fs.statSync(target).mode & 0o777; // keep the user's chosen permissions on an existing file
  } catch {
    /* new file → default 0o600 */
  }
  const tmp = path.join(dir, `.${path.basename(target)}.decompute.${process.pid}.tmp`);
  fs.writeFileSync(tmp, text, { mode });
  try {
    fs.renameSync(tmp, target); // same-dir rename → atomic, single filesystem, preserves the symlink at `file`
  } catch (e) {
    try {
      fs.unlinkSync(tmp);
    } catch {
      /* ignore cleanup failure */
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
    /* best effort — backup is a safety net, not a hard requirement */
  }
}
