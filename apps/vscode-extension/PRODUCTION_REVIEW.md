# Decompute VS Code extension — production readiness review

Scenario matrix + findings from a production QA pass (role: production manager). Severity:
🔴 critical (data loss / breaks Claude Code) · 🟠 high · 🟡 medium · 🟢 polish.

## Findings

| # | Sev | Scenario | Issue | Fix |
|---|-----|----------|-------|-----|
| 1 | 🔴 | `~/.claude/settings.json` has comments/JSONC or is corrupt, user enables routing | `readJson` returns `{}` for *both* missing and unparseable → `wireClaudeSettings` overwrites the whole file, **wiping theme/permissions/hooks** | `settings.ts` planWire: distinguish missing (create) vs unparseable (abort + warn); never overwrite a file that failed to parse |
| 2 | 🟠 | VS Code crashes mid-write | Non-atomic `writeFileSync` can leave a half-written Claude config | `writeFileAtomic` (temp + rename) + one-time `.decompute-backup` |
| 3 | 🟠 | User enables routing, then reloads the window | `deactivate()` unwires settings.json on **every** reload → routing "keeps turning itself off" | deactivate no longer unwires; routing removed only on explicit Disable Routing; activate + config-change self-heal keep it consistent |
| 4 | 🟠 | "Disable Routing" while a Claude Code session/terminal is already running | Running sessions captured `ANTHROPIC_BASE_URL` at launch + the terminal env-collection persists → **gateway keeps receiving requests when "off"** | Can't kill running sessions (platform limit); disable now warns to reload panel + open new terminals, clears the env-collection, and the status bar shows true on/off state |
| 5 | 🟡 | Routing enabled while gateway is down | No health check → Claude Code breaks with cryptic ECONNREFUSED | `enableRouting` health-checks `/health`; warns + lets user confirm |
| 6 | 🟡 | `~/.decompute` not writable | `loadDeviceId()` mints a **new** id each call → unstable device identity / license/quota breakage | cache device id in-memory for the session |
| 7 | 🟡 | Gateway returns non-JSON (proxy/HTML error) | `api()` `JSON.parse` throws | safe parse + better error message |
| 8 | 🟡 | `routeClaudeCodePanel` unset | default inconsistent (`true` in `wireClaudeSettings`, `false` elsewhere) | normalize to `false` everywhere |
| 9 | 🟡 | Free quota exhausted | enforcement path correctly degrades to `mode=off` and **still sends the prompt** ✅; but the read-only `/v1/license/status` path reports `free_quota_available` at 0 remaining → wrong status-bar quota | gateway `licensing._resolve(consume=False)` should report exhausted when remaining==0 |
| 10 | 🟢 | Can't tell if routing is on | no UI signal | status bar shows `Decompute Pro/▢ off`, tooltip states routing on/off + warns if on-but-gateway-down |

## Verified working (no change)
- Quota-exhausted **still forwards the prompt** to Claude (engine=`off`), so the user can always send. ✅ (live-tested at 0/100)
- Upstream key resolution prefers the gateway's `.env` key (BYOK). ✅
- Commands/config IDs match `package.json`; extension installed + compiles. ✅

## Test plan (pure logic via Node `node:test`, in `test/settings.test.mjs`)
planWire: missing→created · valid→merged (preserves other keys) · already-wired→nochange · JSONC/corrupt→unparseable (no write) · array/string→unparseable.
planUnwire: removes only managed keys, preserves user env + other settings · no-env→nochange · corrupt→untouched.
writeFileAtomic round-trips; readFileOrNull returns null only on ENOENT.
