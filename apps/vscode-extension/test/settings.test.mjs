import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { planWire, planUnwire, readFileOrNull, writeFileAtomic } from "../dist/settings.mjs";

const BASE = "http://127.0.0.1:8080";
const HDR = "x-decompute-device: dev-test";

// ── planWire ────────────────────────────────────────────────────────────────
test("planWire: missing file -> creates one with just the env block", () => {
  const r = planWire(null, BASE, HDR);
  assert.equal(r.status, "created");
  const o = JSON.parse(r.text);
  assert.equal(o.env.ANTHROPIC_BASE_URL, BASE);
  assert.equal(o.env.ANTHROPIC_CUSTOM_HEADERS, HDR);
});

test("planWire: valid file -> merges and preserves the user's other settings", () => {
  const cur = JSON.stringify({ theme: "dark", effortLevel: "max", permissions: { allow: ["Bash"] } });
  const r = planWire(cur, BASE, HDR);
  assert.equal(r.status, "wired");
  const o = JSON.parse(r.text);
  assert.equal(o.theme, "dark");
  assert.equal(o.effortLevel, "max");
  assert.deepEqual(o.permissions, { allow: ["Bash"] });
  assert.equal(o.env.ANTHROPIC_BASE_URL, BASE);
});

test("planWire: preserves the user's own env vars alongside ours", () => {
  const o = JSON.parse(planWire(JSON.stringify({ env: { FOO: "bar" } }), BASE, HDR).text);
  assert.equal(o.env.FOO, "bar");
  assert.equal(o.env.ANTHROPIC_BASE_URL, BASE);
});

test("planWire: already wired -> nochange, no rewrite", () => {
  const cur = JSON.stringify({ env: { ANTHROPIC_BASE_URL: BASE, ANTHROPIC_CUSTOM_HEADERS: HDR } });
  const r = planWire(cur, BASE, HDR);
  assert.equal(r.status, "already");
  assert.equal(r.text, null);
});

test("planWire: JSONC/comments -> unparseable, NEVER overwrites (the data-loss guard)", () => {
  const r = planWire(`{\n  // my prefs\n  "theme": "dark"\n}`, BASE, HDR);
  assert.equal(r.status, "unparseable");
  assert.equal(r.text, null);
});

test("planWire: array/string top-level -> unparseable (won't clobber)", () => {
  assert.equal(planWire("[1,2,3]", BASE, HDR).status, "unparseable");
  assert.equal(planWire('"hi"', BASE, HDR).status, "unparseable");
});

// ── planUnwire ──────────────────────────────────────────────────────────────
test("planUnwire: removes only our managed keys, preserves the rest", () => {
  const cur = JSON.stringify({ theme: "dark", env: { ANTHROPIC_BASE_URL: BASE, ANTHROPIC_CUSTOM_HEADERS: HDR, FOO: "bar" } });
  const r = planUnwire(cur);
  assert.equal(r.status, "removed");
  const o = JSON.parse(r.text);
  assert.equal(o.theme, "dark");
  assert.equal(o.env.FOO, "bar");
  assert.ok(!("ANTHROPIC_BASE_URL" in o.env));
  assert.ok(!("ANTHROPIC_CUSTOM_HEADERS" in o.env));
});

test("planUnwire: env left empty -> the env key is dropped, other settings kept", () => {
  const cur = JSON.stringify({ theme: "dark", env: { ANTHROPIC_BASE_URL: BASE, ANTHROPIC_CUSTOM_HEADERS: HDR } });
  const o = JSON.parse(planUnwire(cur).text);
  assert.ok(!("env" in o));
  assert.equal(o.theme, "dark");
});

test("planUnwire: nothing of ours / missing -> nochange", () => {
  assert.equal(planUnwire(JSON.stringify({ theme: "dark" })).status, "nochange");
  assert.equal(planUnwire(null).status, "nochange");
});

test("planUnwire: unparseable -> left untouched (no write)", () => {
  const r = planUnwire(`{ // comment\n "theme":"dark"}`);
  assert.equal(r.status, "unparseable");
  assert.equal(r.text, null);
});

test("planWire: env present but not a plain object (array) -> unparseable, never clobbers", () => {
  const r = planWire(JSON.stringify({ theme: "dark", env: ["weird"] }), BASE, HDR);
  assert.equal(r.status, "unparseable");
  assert.equal(r.text, null);
});

// ── atomic IO ───────────────────────────────────────────────────────────────
test("writeFileAtomic + readFileOrNull round-trip; missing -> null", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dctest-"));
  const f = path.join(dir, "settings.json");
  assert.equal(readFileOrNull(f), null);
  writeFileAtomic(f, '{"a":1}\n');
  assert.equal(readFileOrNull(f), '{"a":1}\n');
  fs.rmSync(dir, { recursive: true, force: true });
});

test("writeFileAtomic writes THROUGH a symlink (preserves the link, updates the backing file)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dctest-"));
  const real = path.join(dir, "real-settings.json");
  const link = path.join(dir, "settings.json");
  fs.writeFileSync(real, '{"theme":"dark"}\n');
  fs.symlinkSync(real, link);
  writeFileAtomic(link, '{"theme":"dark","env":{"X":"1"}}\n');
  assert.ok(fs.lstatSync(link).isSymbolicLink(), "symlink must be preserved, not replaced by a file");
  assert.equal(fs.readFileSync(real, "utf8"), '{"theme":"dark","env":{"X":"1"}}\n', "backing file must be updated");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("writeFileAtomic preserves an existing file's permissions", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dctest-"));
  const f = path.join(dir, "settings.json");
  fs.writeFileSync(f, "{}\n", { mode: 0o644 });
  fs.chmodSync(f, 0o644);
  writeFileAtomic(f, '{"a":1}\n');
  assert.equal(fs.statSync(f).mode & 0o777, 0o644, "should keep 0644, not force 0600");
  fs.rmSync(dir, { recursive: true, force: true });
});
