import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { planWire, planUnwire, readFileOrNull, writeFileAtomic } from "../dist/settings.mjs";

const BASE = "http://127.0.0.1:8080";
const HDR = "x-decompute-device: dev-test";

test("planWire: missing file -> creates env block", () => {
  const r = planWire(null, BASE, HDR);
  assert.equal(r.status, "created");
  const o = JSON.parse(r.text);
  assert.equal(o.env.ANTHROPIC_BASE_URL, BASE);
  assert.equal(o.env.ANTHROPIC_CUSTOM_HEADERS, HDR);
});

test("planWire (enable): merges + preserves the user's other settings", () => {
  const r = planWire(JSON.stringify({ theme: "dark", effortLevel: "max" }), BASE, HDR);
  assert.equal(r.status, "wired");
  const o = JSON.parse(r.text);
  assert.equal(o.theme, "dark");
  assert.equal(o.effortLevel, "max");
  assert.equal(o.env.ANTHROPIC_BASE_URL, BASE);
});

test("planWire: already wired -> nochange (no rewrite)", () => {
  const cur = JSON.stringify({ env: { ANTHROPIC_BASE_URL: BASE, ANTHROPIC_CUSTOM_HEADERS: HDR } });
  const r = planWire(cur, BASE, HDR);
  assert.equal(r.status, "already");
  assert.equal(r.text, null);
});

test("planWire: JSONC/corrupt -> unparseable, NEVER overwrites (data-loss guard)", () => {
  assert.equal(planWire(`{\n // c\n "theme":"dark"\n}`, BASE, HDR).status, "unparseable");
  assert.equal(planWire("[1,2]", BASE, HDR).status, "unparseable");
});

test("planUnwire (disable): removes managed keys, keeps the rest", () => {
  const cur = JSON.stringify({ theme: "dark", env: { ANTHROPIC_BASE_URL: BASE, ANTHROPIC_CUSTOM_HEADERS: HDR, FOO: "bar" } });
  const r = planUnwire(cur);
  assert.equal(r.status, "removed");
  const o = JSON.parse(r.text);
  assert.equal(o.theme, "dark");
  assert.equal(o.env.FOO, "bar");
  assert.ok(!("ANTHROPIC_BASE_URL" in o.env));
});

test("enable -> disable round-trip restores the original (minus our keys)", () => {
  const start = JSON.stringify({ theme: "dark" }, null, 2) + "\n";
  const wired = planWire(start, BASE, HDR).text;
  const o = JSON.parse(planUnwire(wired).text);
  assert.equal(o.theme, "dark");
  assert.ok(!("env" in o)); // env emptied of our keys -> dropped
});

test("planUnwire: nothing of ours / missing -> nochange; corrupt -> untouched", () => {
  assert.equal(planUnwire(JSON.stringify({ theme: "dark" })).status, "nochange");
  assert.equal(planUnwire(null).status, "nochange");
  assert.equal(planUnwire(`{ // c\n "a":1}`).status, "unparseable");
});

test("writeFileAtomic + readFileOrNull round-trip; missing -> null; symlink preserved", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dcset-"));
  const real = path.join(dir, "real.json"), link = path.join(dir, "settings.json");
  assert.equal(readFileOrNull(link), null);
  fs.writeFileSync(real, "{}\n");
  fs.symlinkSync(real, link);
  writeFileAtomic(link, '{"a":1}\n');
  assert.ok(fs.lstatSync(link).isSymbolicLink(), "symlink preserved");
  assert.equal(fs.readFileSync(real, "utf8"), '{"a":1}\n', "backing file updated");
  fs.rmSync(dir, { recursive: true, force: true });
});
