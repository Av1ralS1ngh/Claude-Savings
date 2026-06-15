import { test } from "node:test";
import assert from "node:assert/strict";
import {
  cliBinCandidates, configFiles, claudeKeyFile, configDir, resolveInstallCommand,
  DEFAULT_INSTALL_COMMAND, DEFAULT_INSTALL_COMMAND_WINDOWS, shQuote, shQuoteWin, shQuoteFor,
  detectEnv, nextSetupStep, stepLabel,
} from "../dist/setup.mjs";

test("cliBinCandidates: PATH shim first, venv fallback second", () => {
  const c = cliBinCandidates("/home/u", {});
  assert.deepEqual(c, ["/home/u/.local/bin/decompute", "/home/u/.decompute/venv/bin/decompute"]);
});

test("cliBinCandidates: honors installer env overrides (DECOMPUTE_BIN / DECOMPUTE_HOME)", () => {
  const c = cliBinCandidates("/home/u", { DECOMPUTE_BIN: "/opt/bin", DECOMPUTE_HOME: "/opt/dc" });
  assert.deepEqual(c, ["/opt/bin/decompute", "/home/u/.local/bin/decompute", "/opt/dc/venv/bin/decompute"]);
});

test("cliBinCandidates: Windows uses venv\\Scripts\\decompute.exe (no ~/.local/bin)", () => {
  const c = cliBinCandidates("C:\\Users\\u", {}, "win32");
  // path.join with posix separators on a mac runner keeps "/", which is fine — VS Code on Windows
  // accepts both; the key invariants are the Scripts dir, the .exe suffix, and no ~/.local/bin entry.
  assert.equal(c.length, 1);
  assert.ok(c[0].endsWith("decompute.exe"), c[0]);
  assert.ok(c[0].includes("Scripts"), c[0]);
  assert.ok(!c.some((p) => p.includes(".local")), "no POSIX shim dir on Windows");
});

test("cliBinCandidates: Windows honors DECOMPUTE_BIN as the .exe dir", () => {
  const c = cliBinCandidates("C:\\Users\\u", { DECOMPUTE_BIN: "D:\\dc\\bin" }, "win32");
  assert.ok(c[0].endsWith("decompute.exe"), c[0]);
  assert.ok(c[0].startsWith("D:\\dc\\bin"), c[0]);
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

test("resolveInstallCommand: Windows default is the PowerShell install.ps1 one-liner", () => {
  assert.equal(resolveInstallCommand("", "win32"), DEFAULT_INSTALL_COMMAND_WINDOWS);
  assert.equal(resolveInstallCommand(null, "win32"), DEFAULT_INSTALL_COMMAND_WINDOWS);
  assert.match(DEFAULT_INSTALL_COMMAND_WINDOWS, /powershell.*install\.ps1.*iex/i);
  // POSIX default still wins when platform is darwin/linux
  assert.equal(resolveInstallCommand("", "darwin"), DEFAULT_INSTALL_COMMAND);
  // a user override beats the OS default on every platform
  assert.equal(resolveInstallCommand("my-installer", "win32"), "my-installer");
});

test("shQuoteWin: PowerShell single-quote escaping (doubled quote), safe tokens pass", () => {
  assert.equal(shQuoteWin("http://127.0.0.1:8080"), "http://127.0.0.1:8080");
  assert.equal(shQuoteWin("C:\\Users\\a\\decompute.exe"), "C:\\Users\\a\\decompute.exe");
  assert.equal(shQuoteWin(""), "''");
  assert.equal(shQuoteWin("a b"), "'a b'");
  // PowerShell injection chars are inert inside single quotes; a literal ' is doubled
  assert.equal(shQuoteWin("x; rm $env:X"), "'x; rm $env:X'");
  assert.equal(shQuoteWin("it's"), "'it''s'");
});

test("shQuoteFor: picks PowerShell quoting on win32, POSIX elsewhere", () => {
  assert.equal(shQuoteFor("it's", "win32"), "'it''s'");
  assert.equal(shQuoteFor("it's", "darwin"), "'it'\\''s'");
});

test("detectEnv: native windows / WSL / macOS / linux", () => {
  assert.deepEqual(detectEnv({ platform: "win32" }), { kind: "windows", isWsl: false, label: "Windows", platform: "win32" });
  // WSL via remoteName
  assert.deepEqual(detectEnv({ platform: "linux", remoteName: "wsl-ubuntu" }), { kind: "wsl", isWsl: true, label: "Windows (WSL)", platform: "linux" });
  // WSL via os.release() microsoft marker (remoteName absent)
  assert.equal(detectEnv({ platform: "linux", osRelease: "5.15.0-microsoft-standard-WSL2" }).kind, "wsl");
  // a real linux box is NOT WSL
  assert.deepEqual(detectEnv({ platform: "linux", osRelease: "6.1.0-generic" }), { kind: "linux", isWsl: false, label: "Linux", platform: "linux" });
  assert.deepEqual(detectEnv({ platform: "darwin" }), { kind: "macos", isWsl: false, label: "macOS", platform: "darwin" });
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
  assert.equal(nextSetupStep({ installed: true, configured: true, keyPresent: false, routed: false, authMode: "claude_subscription" }), "route");
  assert.equal(nextSetupStep({ installed: true, configured: true, keyPresent: true, routed: false }), "route");
  assert.equal(nextSetupStep({ installed: true, configured: true, keyPresent: true, routed: true }), "done");
});

test("stepLabel covers every step", () => {
  for (const s of ["install", "connect-key", "route", "verify", "done"]) {
    assert.ok(stepLabel(s).length > 0);
  }
});
