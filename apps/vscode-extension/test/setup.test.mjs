import { test } from "node:test";
import assert from "node:assert/strict";
import {
  cliBinCandidates, configFiles, claudeKeyFile, configDir, resolveInstallCommand,
  DEFAULT_INSTALL_COMMAND, shQuote, nextSetupStep, stepLabel,
} from "../dist/setup.mjs";

test("cliBinCandidates: PATH shim first, venv fallback second", () => {
  const c = cliBinCandidates("/home/u", {});
  assert.deepEqual(c, ["/home/u/.local/bin/decompute", "/home/u/.decompute/venv/bin/decompute"]);
});

test("cliBinCandidates: honors installer env overrides (DECOMPUTE_BIN / DECOMPUTE_HOME)", () => {
  const c = cliBinCandidates("/home/u", { DECOMPUTE_BIN: "/opt/bin", DECOMPUTE_HOME: "/opt/dc" });
  assert.deepEqual(c, ["/opt/bin/decompute", "/home/u/.local/bin/decompute", "/opt/dc/venv/bin/decompute"]);
});

test("configFiles + claudeKeyFile point under the config dir (env-overridable)", () => {
  assert.deepEqual(configFiles("/home/u", {}), ["/home/u/.decompute/config.toml", "/home/u/.decompute/config.json"]);
  assert.equal(claudeKeyFile("/home/u", {}), "/home/u/.decompute/claude.key");
  assert.equal(configDir("/home/u", { DECOMPUTE_CONFIG_DIR: "/custom/cfg" }), "/custom/cfg");
  assert.equal(claudeKeyFile("/home/u", { DECOMPUTE_CONFIG_DIR: "/custom/cfg" }), "/custom/cfg/claude.key");
});

test("resolveInstallCommand: override wins, blank -> default", () => {
  assert.equal(resolveInstallCommand("bash /repo/scripts/install.sh"), "bash /repo/scripts/install.sh");
  assert.equal(resolveInstallCommand("  pipx install decompute-ai  "), "pipx install decompute-ai");
  assert.equal(resolveInstallCommand(""), DEFAULT_INSTALL_COMMAND);
  assert.equal(resolveInstallCommand(null), DEFAULT_INSTALL_COMMAND);
  assert.equal(resolveInstallCommand(undefined), DEFAULT_INSTALL_COMMAND);
});

test("shQuote: safe tokens pass through; spaces/quotes/injection get quoted", () => {
  assert.equal(shQuote("https://gateway.decompute.run"), "https://gateway.decompute.run");
  assert.equal(shQuote("/Users/a/.local/bin/decompute"), "/Users/a/.local/bin/decompute");
  assert.equal(shQuote(""), "''");
  assert.equal(shQuote("a b"), "'a b'");
  // command injection attempt is neutralized inside single quotes
  assert.equal(shQuote("x; rm -rf ~"), "'x; rm -rf ~'");
  assert.equal(shQuote("it's"), "'it'\\''s'");
});

test("nextSetupStep walks the funnel in order", () => {
  const base = { installed: false, configured: false, keyPresent: false, routed: false };
  assert.equal(nextSetupStep(base), "install");
  assert.equal(nextSetupStep({ ...base, installed: true }), "connect-key");
  assert.equal(nextSetupStep({ ...base, installed: true, configured: true }), "connect-key"); // key still missing
  assert.equal(nextSetupStep({ installed: true, configured: true, keyPresent: true, routed: false }), "route");
  assert.equal(nextSetupStep({ installed: true, configured: true, keyPresent: true, routed: true }), "done");
});

test("stepLabel covers every step", () => {
  for (const s of ["install", "connect-key", "route", "verify", "done"]) {
    assert.ok(stepLabel(s).length > 0);
  }
});
