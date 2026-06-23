# Teacher-Student Integration Plan

Status: implementation-ready plan for `/Users/avii/Documents/Claude-Coder`

Raw reference bundle only: `/Users/avii/Downloads/decompute_teacher_student_gateway`

Goal: add an optional local coding student to the Decompute gateway without breaking Claude Code, OpenClaw, SDK, compression, billing, privacy, or the existing Anthropic-compatible request paths. The student-teacher flow is invoked only from the explicit commands named in the boss handoff, not from ordinary chat/proxy traffic.

## Executive Decision

Claude's design is the baseline. The boss-shared folder is useful evidence that a simple teacher-student flow can run, but it is raw, incomplete, and not authoritative. Do not copy it into this repo. Use it only as a sketch for local OpenAI-compatible model calls, prompt ideas, and smoke-test shape, then reimplement the integration against the current gateway's real architecture.

The first production-safe ship is command-gated only:

- CLI command: `decompute ts-run`
- CLI health command: `decompute ts-health`
- direct local debug command: `decompute local-chat`
- optional VS Code/MCP command: "Ask local student" or "Teacher-student solve"

The gateway API routes are implementation plumbing behind those commands:

- `GET /v1/teacher_student/health`
- `POST /v1/teacher_student/run`

The existing paths stay unchanged by default:

- `POST /v1/messages`
- `POST /v1/chat/completions`
- `POST /v1/messages/count_tokens`
- `GET /v1/models`

No automatic `/v1/messages` student routing ships until shadow metrics, verifier calibration, sandboxing, and legal/data-policy questions are resolved. In V1, normal prompts never trigger the local student; the user must type a command.

## Source Of Truth

Use this priority order when implementation details conflict:

1. Current `Claude-Coder` codebase behavior and production safety.
2. Claude's architectural plan: command/explicit-use first, layered verifier, governed capture, Yellowstone integration, no hidden fallback.
3. This document, which turns that plan into concrete phases and file-level acceptance criteria.
4. The boss-shared reference folder, as a raw prototype only.

The reference folder should never override the Claude plan or the current repo's safety contracts.

## Draft Refinements

Claude's plan had the right direction. This document keeps that direction and closes the places where an implementer could still make a wrong turn:

1. Preserve the boss handoff's command names. User-facing commands are `decompute ts-run`, `decompute ts-health`, `decompute local-chat`, and the VS Code/MCP command names "Ask local student" / "Teacher-student solve"; gateway plumbing uses `/v1/teacher_student/*`.
2. Keep `student_first` as the only V1 live request mode; keep `teacher_first` and `parallel` for offline eval/calibration.
3. Keep compression savings and teacher-student deflection metrics separate.
4. Make raw reference-bundle hazards explicit: mock fallback, SQLite-only schema, permissive CORS, bare `subprocess`, default-on endpoints, and easy raw-trace storage.
5. Fix the current Yellowstone workload-class mismatch before any observe or auto-route work.
6. Treat Claude-output distillation as blocked until written legal/product approval changes the current repo policy.
7. Ship a narrow safe runtime first, then add persistence, sandboxing, observe mode, CLI, and only later governed exports.

This rewrite makes one canonical plan.

## Claude Plan Principles To Preserve

These are the parts of Claude's plan that are intentionally kept:

1. Command-gated first.
   Users invoke teacher-student deliberately with the boss-handoff CLI, VS Code, or MCP commands. Existing Claude Code traffic is not silently changed.

2. Claude remains the agentic teacher.
   The local student is a narrow coding assistant. It does not become the tool-calling agent, does not receive tool-use turns, and does not replace Claude Code's loop.

3. Verification is layered, not vibes.
   Structure checks, optional sandboxed tests, optional independent judge, and calibrated thresholds are separate signals. A single high-looking score is not enough.

4. Failure is loud and observable.
   Student outage, timeout, over-context, unsupported tools, sandbox unavailable, and judge unavailable are recorded as explicit outcomes.

5. Distillation is governed and delayed.
   The runtime can be useful before training/export exists. Training data capture is separate, opt-in, privacy-gated, and blocked for Claude targets until policy is resolved.

6. Yellowstone remains the routing brain.
   Teacher-student eligibility plugs into planner/policy/device capability instead of creating a separate router.

7. Local runtime is user-controlled by default.
   No model weights are downloaded during gateway startup, and no sidecar process is started unless the user opts into lifecycle management.

## Non-Negotiable Invariants

These are release blockers. If any are violated, the PR should not merge.

1. Existing Claude paths are stable by default.
   `/v1/messages` and `/v1/chat/completions` must behave the same unless a future explicit opt-in is present and accepted.

2. The local student is optional and off by default.
   The existing `DECOMPUTE_LOCAL_STUDENT_ENABLED=false` remains the master switch.

3. No silent mock fallback.
   A down student returns `student_unavailable` for direct local calls or escalates to the teacher for teacher-student calls. It never returns a canned answer as if it came from the model.

4. No tool-use turns go to the student.
   If request body has `tools`, `tool_use`, or `tool_result`, the student is ineligible. Claude stays the tool-capable agent.

5. No prompt-cache agent sessions go to the student automatically.
   Claude Code cache-control sessions often save more through Anthropic prompt caching than local routing would. Preserve the current cache-protection behavior.

6. Test execution is never bare subprocess on the request path.
   Default is disabled. Production requires a real sandbox backend. If no sandbox is available, verifier confidence is capped and the answer usually escalates.

7. Privacy is enforced at the storage boundary.
   Default persistence is metadata-only. Content capture is opt-in, scrubbed, retention-bound, and never sent through weekly telemetry.

8. Claude outputs are not training targets in V1.
   They may be used transiently to answer the request and, if governance permits, recorded as operational traces. They must not be exported as model-training labels until the `CLAUDE.md` policy conflict is resolved in writing.

9. Usage recording cannot crash user requests.
   Follow the existing `_record_usage` pattern: best effort, try/except, route metadata only.

10. All new public surfaces require the existing auth/scopes.
    No unauthenticated dashboards, exports, or local model pass-through on public deployments.

11. Command invocation is the only V1 user trigger.
    Normal Claude Code prompts, OpenClaw requests, SDK calls, and `/v1/messages` traffic must not call the student. The teacher-student API is called by command wrappers, not by the proxy path.

## Current Repo Facts To Build On

Verified in `/Users/avii/Documents/Claude-Coder`:

- `apps/api/decompute_gateway/config.py`
  Existing flags:
  - `local_student_enabled`
  - `local_student_training_enabled`
  Production already rejects `local_student_training_enabled`.

- `apps/api/decompute_gateway/yellowstone/model_registry.py`
  Existing model profile:
  - `local-student-coder`
  - provider `local`
  - context window `32000`
  - no prompt cache
  - no tool use
  - `local_weight_gb=8.0`

- `apps/api/decompute_gateway/yellowstone/policy.py`
  Existing route placeholder:
  - `local_student_dry_run`
  - currently gated on workload classes the real classifier does not emit.

- `apps/api/decompute_gateway/workload.py`
  Real emitted classes:
  - `structured_json`
  - `html`
  - `logs`
  - `source_code`
  - `prose`
  - `short`
  - `mixed`

- `apps/api/decompute_gateway/main.py`
  Existing request pipeline already handles:
  - compression
  - CCR
  - prompt-cache preservation
  - planner route metadata
  - Anthropic provider calls
  - stream cleanup
  - usage recording
  - `detect_client(request)`

- `apps/api/decompute_gateway/db.py` and `models.py`
  `CLIENT_BUCKETS` is now enforced at the storage boundary for usage client tags. Preserve that pattern for teacher-student enums.

- `CLAUDE.md`
  Explicit policy: do not train or fine-tune models on Claude API responses. Treat this as blocking for Claude-target distillation.

## Raw Reference Bundle: What To Reuse And What To Reject

Reusable ideas from `/Users/avii/Downloads/decompute_teacher_student_gateway`:

- OpenAI-compatible local model client.
- Local GGUF sidecar default on `127.0.0.1:18080/v1`.
- Basic teacher-student service flow.
- Prompt templates for student and teacher correction.
- Smoke-test script structure.
- Health endpoint concept.
- Command-triggered UX. The handoff mentions `decompute ts-health`, `decompute ts-run`, `decompute local-chat`, and a VS Code/MCP command such as "Ask local student" or "Teacher-student solve". It does not define the exact `/student-teacher` slash command, but it supports the same direction: explicit command invocation only.

Do not copy these behaviors:

- `student_mock_fallback=true` or `teacher_mock_fallback=true` defaults.
- Bare `subprocess.run` for user-supplied tests.
- SQLite hand-written schema.
- Direct `Store(...)` APIs that do not match this repo's SQLAlchemy store.
- Permissive public dashboard/CORS defaults.
- Default-on teacher-student endpoints.
- Distillation export that can include Claude answers by default.
- Streaming branches that bypass this repo's `streaming_cleanup.py`.

Implementation rule: if a reference-bundle file disagrees with this plan, this plan wins. If this plan disagrees with the live `Claude-Coder` code, update the plan or implementation intentionally rather than forcing the raw prototype shape into production code.

## Target Architecture

```text
Claude Code / OpenClaw / SDK
        |
        | existing Anthropic/OpenAI-compatible traffic
        v
Decompute Gateway on 127.0.0.1:8080
        |
        | existing paths
        | /v1/messages and /v1/chat/completions
        v
Compression + Yellowstone + Anthropic teacher

Command-gated teacher-student path only:

Client
        |
        | decompute ts-run or VS Code/MCP "Teacher-student solve"
        v
Command wrapper
        |
        | local gateway call: /v1/teacher_student/run
        v
TeacherStudentService
        |
        +--> Local student sidecar, OpenAI-compatible
        |    http://127.0.0.1:18080/v1/chat/completions
        |
        +--> Layered verifier
        |
        +--> Existing Anthropic teacher path only when needed
        |
        +--> usage_events + governed teacher-student tables
```

## Canonical Command Surface

The user-facing V1 surface is command-based. The user should never need to call `/v1/teacher_student/run` directly.

Canonical commands:

- `decompute ts-run "<task>"`.
- `decompute ts-health`.
- `decompute local-chat "<prompt>"` for direct local-student debugging only.
- optional VS Code/MCP command: "Ask local student".
- optional VS Code/MCP command: "Teacher-student solve".

Do not rename these in V1. Longer names such as `decompute teacher-student run` can be added later as aliases, but the boss-handoff commands above are the product vocabulary to preserve.

Command invocation metadata:

- Every command wrapper sends `source` as one of `cli`, `vscode_command`, or `mcp_tool`.
- The wrapper adds a bounded command name, for example `ts-run`, `ts-health`, `local-chat`, `ask_local_student`, or `teacher_student_solve`.
- The gateway stores only the bounded source/command bucket in route metadata, never a raw terminal line.
- Normal `/v1/messages` requests do not set this source and must not invoke the student.

## Internal Gateway API Surface

Use only `/v1/teacher_student/*` for backend plumbing. Do not also create `/v1/student/*`.

### `GET /v1/teacher_student/health`

Scope: `routes:read`

Returns:

- enabled flag
- student backend/config summary
- health status and latency
- model loaded if backend reports it
- verifier config summary
- sandbox status
- capture/governance mode

Must not return prompts, code, API keys, raw request bodies, or local filesystem paths beyond configured model/base URL.

### `POST /v1/teacher_student/run`

Scope: `messages:write`

Internal endpoint used by the command wrappers. Non-streaming in V1 because verified routing needs the complete student answer before deciding whether to escalate.

Request shape:

```json
{
  "name": "optional-run-name",
  "task": "Implement add(a, b).",
  "messages": [{"role": "user", "content": "Implement add(a, b)."}],
  "tests": "assert add(2, 3) == 5",
  "mode": "student_first",
  "require_teacher": false,
  "max_tokens": 2048,
  "temperature": 0.2,
  "source": "cli",
  "command": "ts-run",
  "decompute": {
    "teacher_student": {
      "capture": false
    }
  }
}
```

Response shape:

```json
{
  "id": "tsc_...",
  "route": "student_only",
  "final_answer": "...",
  "student_answer": "...",
  "teacher_answer": null,
  "verdict": {
    "decision": "accept_student",
    "confidence": 0.91,
    "escalation_reason": null,
    "layers": []
  },
  "usage": {
    "input_tokens": 1234,
    "student_output_tokens": 300,
    "teacher_output_tokens": 0,
    "student_latency_ms": 8200,
    "teacher_latency_ms": 0,
    "teacher_called": false,
    "estimated_teacher_cost_avoided_usd": 0.0042
  },
  "request_id": "dreq_...",
  "usage_event_id": "use_..."
}
```

Routes:

- `student_only`: student accepted.
- `teacher_corrected`: student attempted, verifier escalated, teacher answer returned.
- `teacher_only`: student skipped because ineligible or unavailable and teacher answered.
- `student_unavailable`: direct student path failed and teacher was not allowed/configured.
- `disabled`: feature flag off.

Status code rules:

- feature disabled: `503` with `{"error":"teacher_student_disabled"}`
- student unavailable and teacher fallback allowed/configured: `200` with route `teacher_only`
- student unavailable and teacher fallback unavailable: `503` with `{"error":"student_unavailable"}`
- unsupported student request, such as tools present: `200` with route `teacher_only`
- invalid request shape: `400`
- auth/scope failure: existing auth error behavior

### `POST /v1/teacher_student/chat/completions`

Scope: `messages:write`

Deferred in V1 unless a command wrapper actually needs it. If implemented, this is still backend plumbing for explicit command/tool clients, not a replacement for the existing `/v1/chat/completions`.

Do not route the existing `/v1/chat/completions` endpoint here.

### Deferred Endpoints

Ship after V1 is stable:

- `POST /v1/teacher_student/batch`
- `GET /v1/teacher_student/runs`
- `GET /v1/teacher_student/cases`
- `GET /v1/teacher_student/export/sft.jsonl`
- `GET /v1/teacher_student/export/preference.jsonl`
- `POST /v1/teacher_student/purge_expired`

These remain backend/admin plumbing. If surfaced to users, surface them through commands such as `decompute ts-export`, not through normal chat/proxy paths. Export endpoints require separate scopes and governance work. Do not include them in the first live routing PR unless the capture policy is finished.

## Mode Semantics

Request path modes:

- `student_first`: only live request mode in V1. Try student, verify, escalate if risky.
- `distill`: not a routing mode. It is a capture/export intent layered on top of `student_first`, and it is disabled until governance and legal sign-off allow it.

Offline/eval modes:

- `teacher_first`: offline benchmarking only.
- `parallel`: offline calibration only.

Do not expose `teacher_first` or `parallel` on live `/v1/teacher_student/run` in V1. They intentionally add teacher cost and latency.

## Provider Design

Add:

- `apps/api/decompute_gateway/local_provider.py`
- tests in `tests/unit/test_local_provider.py`

Keep it top-level beside `anthropic_provider.py` for now. A new `providers/` package would be an unnecessary refactor.

### Local Provider Contract

The local provider speaks OpenAI-compatible chat completions:

- llama.cpp server
- Ollama OpenAI-compatible route
- LM Studio
- vLLM
- generic local OpenAI-compatible server

It returns a typed result:

```python
@dataclass(frozen=True)
class LocalProviderResult:
    ok: bool
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    request_id: str | None
    raw_usage: dict[str, Any]
```

Failures raise typed exceptions:

- `LocalProviderUnavailable`
- `LocalProviderTimeout`
- `LocalProviderOverContext`
- `LocalProviderUnsupported`

No exception path returns a mock answer.

### Translation Rules

Input:

- Convert Anthropic-style `system` into an OpenAI `system` message.
- Convert string content and text blocks to text.
- Reject unsupported content blocks.
- Reject `tools`, `tool_use`, and `tool_result` with `LocalProviderUnsupported(reason="tools_present")`.
- Reject context over `local_student_context_window`.

Output:

- Prefer provider `usage.prompt_tokens` and `usage.completion_tokens`.
- Fall back to local `estimate_tokens` when usage is missing.
- Keep raw local response only in memory unless debug capture is explicitly enabled.

### Health, Timeouts, And Concurrency

Settings:

- `local_student_base_url`
- `local_student_backend`
- `local_student_model`
- `local_student_api_key`
- `local_student_context_window`
- `local_student_max_output_tokens`
- `local_student_connect_timeout_s`
- `local_student_read_timeout_s`
- `local_student_overall_timeout_s`
- `local_student_health_ttl_s`
- `local_student_max_concurrency`

Rules:

- Cache health checks for a short TTL.
- Use finite connect/read/overall timeouts.
- Default concurrency is 1.
- Never block the event loop with sync HTTP or CPU-heavy work.
- Student generation runs behind an `asyncio.Semaphore`.

## Settings

Use this repo's `DECOMPUTE_*` naming as canonical. Support old `TS_*` aliases only as temporary compatibility if copying reference scripts.

Add to `Settings` near the existing local-student flags:

```python
local_student_backend: str = os.getenv("DECOMPUTE_LOCAL_STUDENT_BACKEND", "llama_cpp")
local_student_base_url: str = os.getenv("DECOMPUTE_LOCAL_STUDENT_BASE_URL", "http://127.0.0.1:18080/v1")
local_student_model: str = os.getenv("DECOMPUTE_LOCAL_STUDENT_MODEL", "yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q4_K_M")
local_student_api_key: str | None = os.getenv("DECOMPUTE_LOCAL_STUDENT_API_KEY")
local_student_context_window: int = _int("DECOMPUTE_LOCAL_STUDENT_CONTEXT_WINDOW", 32000)
local_student_max_output_tokens: int = _int("DECOMPUTE_LOCAL_STUDENT_MAX_OUTPUT_TOKENS", 2048)
local_student_connect_timeout_s: float = _float("DECOMPUTE_LOCAL_STUDENT_CONNECT_TIMEOUT_S", 3.0)
local_student_read_timeout_s: float = _float("DECOMPUTE_LOCAL_STUDENT_READ_TIMEOUT_S", 60.0)
local_student_overall_timeout_s: float = _float("DECOMPUTE_LOCAL_STUDENT_OVERALL_TIMEOUT_S", 120.0)
local_student_health_ttl_s: float = _float("DECOMPUTE_LOCAL_STUDENT_HEALTH_TTL_S", 15.0)
local_student_max_concurrency: int = _int("DECOMPUTE_LOCAL_STUDENT_MAX_CONCURRENCY", 1)
local_student_mock: bool = _bool("DECOMPUTE_LOCAL_STUDENT_MOCK", False)

teacher_student_sandbox_backend: str = os.getenv("DECOMPUTE_TS_SANDBOX_BACKEND", "disabled")
teacher_student_test_execution_enabled: bool = _bool("DECOMPUTE_TS_TEST_EXECUTION", False)
teacher_student_confidence_threshold: float = _float("DECOMPUTE_TS_CONFIDENCE_THRESHOLD", 0.82)
teacher_student_judge_enabled: bool = _bool("DECOMPUTE_TS_JUDGE_ENABLED", False)
teacher_student_capture_enabled: bool = _bool("DECOMPUTE_TS_CAPTURE_ENABLED", False)
teacher_student_governance_mode: str = os.getenv("DECOMPUTE_TS_GOVERNANCE_MODE", "metadata_only")
teacher_student_capture_retention_days: int = _int("DECOMPUTE_TS_CAPTURE_RETENTION_DAYS", 30)
teacher_student_allow_claude_training_targets: bool = _bool("DECOMPUTE_TS_ALLOW_CLAUDE_TRAINING_TARGETS", False)
```

Production validation:

- reject `local_student_mock=true`
- reject test execution unless sandbox backend is production-safe
- reject `teacher_student_allow_claude_training_targets=true` unless policy/legal sign-off is explicitly encoded
- reject non-loopback `http://` local student base URLs in production
- reject capture modes that store raw text without encryption/redaction settings

## Service Design

Add:

- `apps/api/decompute_gateway/teacher_student/__init__.py`
- `apps/api/decompute_gateway/teacher_student/schemas.py`
- `apps/api/decompute_gateway/teacher_student/service.py`
- `apps/api/decompute_gateway/teacher_student/verifier.py`
- `apps/api/decompute_gateway/teacher_student/prompts.py`
- `apps/api/decompute_gateway/teacher_student/router.py`

`main.py` should only mount the router and inject existing dependencies. Avoid a large `main.py` patch.

Service flow:

1. Authenticate with existing `_auth`.
2. Sanitize optional session/client metadata using existing helpers.
3. Normalize the request into messages.
4. Check eligibility:
   - feature enabled
   - local provider healthy
   - no tools
   - context within limit
   - prompt-cache/tool-agent constraints satisfied
   - workload/coding intent allowed
5. If ineligible, call teacher only or return disabled/unavailable depending on request mode.
6. If eligible, call the local student.
7. Verify student answer.
8. If verifier says accept, return student answer.
9. If verifier says escalate, call existing `call_anthropic_messages` as teacher.
10. Record usage and teacher-student metadata.
11. Persist governed case/verdict metadata.

Teacher calls must reuse:

- `upstream_anthropic_auth(request, settings)`
- `call_anthropic_messages`
- existing retry/sanitization behavior
- existing auth and rate-limit machinery

Do not create a second Anthropic client.

## Verifier Design

The verifier is escalation-biased. A confident-looking wrong answer must not pass because it has a code fence.

Decision rule:

```text
accept student only if:
  no hard-risk layer fired
  and confidence >= threshold
  and confidence was not capped below threshold by missing evidence
otherwise escalate
```

### Layer 0: Deterministic Checks

Always runs. No network, model, or sandbox.

Hard escalate:

- empty answer
- refusal language
- expected code missing
- syntax parse failure for detected language
- tool/repo-edit requirement detected
- output appears truncated
- requested file edits but answer is only prose

Soft risk:

- no tests available
- no code fences for coding task
- very large diff-like output
- unsupported language parser

### Layer 1: Objective Tests

Runs only when:

- tests are supplied or safely generated
- sandbox backend is available
- request explicitly permits test execution

Backends:

- `disabled`: default; no tests run; confidence cap applies.
- `container`: local Docker/Podman/gVisor with no network, read-only base, memory/CPU/PID limits, dropped capabilities, timeout.
- `cloudflare`: hosted sandbox backend if/when the hosted gateway runs where that is available.

Bare `subprocess` is allowed only in a developer-only test helper that is not imported by the FastAPI app and is not reachable from request handling.

If tests fail, escalate.

If tests cannot run safely, do not pretend they passed. Cap confidence.

### Layer 2: Judge

Optional and disabled by default.

Rules:

- The student cannot judge itself.
- Prefer a separate judge model or Claude-as-judge only when the cost tradeoff is acceptable.
- The prompt asks the judge to find flaws, not to praise.
- `fail`, `uncertain`, severity >= 2, or "would not ship" escalates.

### Layer 3: Calibrated Threshold

Initial threshold is conservative, for example `0.82`.

Confidence caps:

- tests passed in sandbox: cap 1.0
- no tests but judge passed: cap 0.85 (= base_confidence; a passing independent skeptical judge is the
  serve signal on the `/v1/messages` proxy path, where requests carry no user tests, so this cap must
  clear the threshold. Kept strictly below the tests-passed cap so executed tests always outrank a
  judge opinion.)
- tests provided but sandbox unavailable: cap 0.60
- no tests and no judge: cap 0.55

This means "no tests, no judge" always escalates (0.55 < 0.82). That is intentional. "No tests but an
independent judge approves" now serves, which is what makes inline serving on the proxy path viable.

## Yellowstone And Routing

Do not make a parallel router.

Add:

- `apps/api/decompute_gateway/yellowstone/student_capability.py`
- optional `apps/api/decompute_gateway/teacher_student/intent.py`

Fix the current dead gate:

- Current policy checks classes `simple_code_patch`, `lint_fix`, `docs_edit`, `test_generation`.
- Current classifier emits only `structured_json`, `html`, `logs`, `source_code`, `prose`, `short`, `mixed`.

V1 solution:

- Keep `workload.py` broad classes for analytics.
- Add a small coding-intent classifier for teacher-student eligibility:
  - `simple_code_patch`
  - `lint_fix`
  - `docs_edit`
  - `test_generation`
  - `explain_code`
  - `unknown`

Eligibility requires:

- no tools
- no prompt-cache critical path
- final tokens under min(student context, configured budget)
- local student healthy
- model/device feasible
- coding intent in the allowlist
- command-gated teacher-student invocation or future opt-in flag

Policy routes:

- `local_student_observe`: record "would have tried student", never serve student.
- `local_student`: allowed only for command-gated invocation or future opt-in.
- existing Claude/compression routes remain unchanged.

In V1, `/v1/messages` may record observe metadata only if it is near-zero overhead and cannot affect response behavior.

## Streaming

Do not stream verified `/v1/teacher_student/run` in V1.

Reason: the service cannot know whether to accept or escalate until the full student answer has been verified. Streaming student bytes before verification would make escalation impossible without sending contradictory output.

Allowed V1 behavior:

- `/v1/teacher_student/run`: non-streaming only.
- `/v1/teacher_student/chat/completions` with `stream=false`: deferred unless needed by a command wrapper.
- Direct unverified local streaming can be a dev-only future command, but it must be clearly labeled unverified and use `streaming_cleanup.py`.

Future V2 options:

- buffer full student answer, verify, then replay accepted answer as Anthropic SSE
- send progress events, not answer tokens, while verifying
- speculative teacher/student parallel streaming only for offline/eval, not default live requests

## Data Model And Migrations

Current Alembic head is `0007_usage_client.py`. Add `0008_teacher_student.py`.

Use SQLAlchemy ORM in `models.py`, not the reference bundle's SQLite schema.

### Minimal V1 Tables

Add project-level capture consent columns to `projects` in the same migration:

- `teacher_student_capture_enabled` boolean, default false
- `teacher_student_governance_mode` string, default `metadata_only`
- `teacher_student_capture_retention_days` integer, default 30
- `teacher_student_capture_locked` boolean, default false

Server config and project config must both allow capture. `teacher_student_capture_locked=true` overrides project opt-in and forces metadata-only behavior.

`teacher_student_runs`

- `id`
- `project_id`
- `name`
- `mode`
- `status`
- `student_model`
- `teacher_model`
- `governance_mode`
- `case_count`
- `teacher_call_count`
- `metadata_json`
- `created_at`
- `updated_at`

`teacher_student_cases`

- `id`
- `run_id`
- `project_id`
- `usage_event_id`
- `request_id`
- `session_id`
- `client`
- `route`
- `workload_class`
- `coding_intent`
- `escalation_reason`
- `storage_mode`
- `prompt_sha256`
- `feature_json`
- `student_answer`
- `teacher_answer`
- `final_answer`
- `tests_json`
- `input_tokens`
- `student_output_tokens`
- `teacher_output_tokens`
- `student_latency_ms`
- `teacher_latency_ms`
- `expires_at`
- `created_at`

`teacher_student_verdicts`

- `id`
- `case_id`
- `verifier_version`
- `decision`
- `confidence`
- `threshold`
- `escalated`
- `escalation_reason`
- `tests_ran`
- `tests_passed`
- `sandbox_backend`
- `judge_model`
- `layers_json`
- `created_at`

Defer `teacher_student_distillation_records` until capture/export is allowed.

### Governance At Store Boundary

Add store methods:

- `create_teacher_student_run(...)`
- `record_teacher_student_case(...)`
- `record_teacher_student_verdict(...)`
- `list_teacher_student_runs(...)`
- `teacher_student_summary(...)`

All enum-like values are clamped at the store boundary:

- mode
- route
- storage mode
- client bucket
- governance mode
- verifier decision

Default `storage_mode="metadata_only"`:

- do not store prompt messages
- do not store tests
- do not store student answer
- do not store teacher answer
- do not store final answer
- store only feature metadata, token counts, hashes, verdict metadata, and route metadata

When capture is explicitly enabled:

- scrub secrets and PII before storage
- record secret scan status
- set retention
- never export Claude outputs as training targets unless policy permits

Regulated mode:

- no raw or redacted content
- hashes/features only
- export returns 409 or empty with a clear reason

### Usage Ledger

Each run writes a normal `usage_events` row via `_record_usage` or a small helper with the same safety guarantees.

Do not overload compression savings:

- `tokens_saved` remains compression/input-token savings.
- teacher-student deflection metrics live in `route_json`:
  - `teacher_called`
  - `teacher_tokens_avoided`
  - `estimated_teacher_cost_avoided_usd`
  - `student_latency_ms`
  - `teacher_latency_ms`
  - `verifier_decision`
  - `case_id`
  - `run_id`

Dashboard can add a separate "Teacher-student deflection" card. The weekly telemetry beacon remains counts-only and should not include teacher-student content.

## Privacy And Security

Default:

- no raw prompts stored
- no raw answers stored
- no tests stored
- no model training export
- no telemetry changes

Capture opt-in:

- project-level and server-level gates both required
- scrub before write
- retention enforced
- export audited
- export scope separated from `messages:write`

New scopes:

- `teacher_student:read`
- `teacher_student:admin`
- `distill:read`
- `distill:admin`

`admin` may imply these, but ordinary `messages:write` must not imply export.

Add these to `db.ALL_SCOPES`, update API-key/admin tests, and document that `messages:write` only permits creating a run, not listing stored cases or exporting records.

Threats to test:

- local provider down must not leak a mock answer
- raw User-Agent must not be stored
- raw prompts must not be stored in metadata-only mode
- test sandbox cannot read `~/.decompute/claude.key`
- test sandbox cannot reach gateway or network when network is disabled
- export cannot return content for regulated mode
- route metadata cannot contain API keys or prompt bodies

## Command UX

Primary user story:

1. User keeps using Claude Code normally.
2. When they want the local student-teacher flow, they run `decompute ts-run ...` or choose the VS Code/MCP command "Teacher-student solve".
3. The command invokes the Decompute CLI or a small command wrapper.
4. The wrapper calls the local gateway's `/v1/teacher_student/run`.
5. The gateway runs student -> verifier -> optional Claude teacher.
6. The command prints the final answer and a short route summary.

Required CLI behavior:

- `decompute ts-run "<task>"`: run one teacher-student coding task.
- `decompute ts-run "<task>" --tests <path-or-inline>`: include tests.
- `decompute ts-run "<task>" --require-teacher`: always get Claude review/correction.
- `decompute ts-run "<task>" --execute-tests`: run tests only when sandbox policy allows it.
- `decompute ts-health`: show student/teacher readiness.
- `decompute local-chat "<prompt>"`: call the configured local student directly for debugging.

Optional command wrappers:

- VS Code command: "Ask local student".
- VS Code command: "Teacher-student solve".
- MCP tool with the same semantics as `decompute ts-run`.

Slash command note:

- The boss handoff did not name a slash command. Do not invent one as the V1 canonical command.
- If Claude Code custom slash commands are later added, make them thin wrappers over `decompute ts-run` and keep the `ts-run` command name in metadata.
- The command must never edit `~/.claude/settings.json` routing beyond the existing Decompute routing setup.

Output style:

- Print the final answer first.
- Then print a compact route line: `student_only`, `teacher_corrected`, `teacher_only`, or `student_unavailable`.
- Show verifier confidence and escalation reason when present.
- Do not print raw API keys, raw headers, or hidden prompt metadata.

## CLI And Local Runtime

Add after gateway API V1:

- `decompute ts-run`
- `decompute ts-health`
- `decompute local-chat`
- `decompute ts-export` later, after governance/export is approved
- `decompute student status`
- `decompute student start`
- `decompute student stop`
- `decompute student install gemma-coder`
- `decompute student pull`
- `decompute student smoke`

Rules:

- no model download during gateway startup
- `pull` shows model name and size, asks for confirmation
- default topology is user-managed local server
- gateway-supervised sidecar is opt-in only
- Windows support uses Task Scheduler/process APIs where needed
- macOS support can use launchd only after local manual start works

Doctor checks:

- backend reachable
- model appears loaded
- configured context window
- free memory estimate
- short generation latency
- expected tokens/sec
- sandbox availability
- current governance/capture settings

## Implementation Phases

### Phase 0: Plan Cleanup And Tests Baseline

Deliverables:

- this plan
- confirm current tests that cover `/v1/messages` and `/v1/chat/completions`
- add regression tests proving existing paths do not call local student when disabled

Acceptance:

- no behavior change
- no new runtime dependency

### Phase 1: Local Provider And Health

Files:

- `apps/api/decompute_gateway/local_provider.py`
- `tests/unit/test_local_provider.py`
- `.env.example`

Acceptance:

- health works with `httpx.MockTransport`
- unavailable local server raises typed exception
- tools are rejected
- context overflow is rejected
- no mock fallback outside an explicit test-only mock

### Phase 2: Command-Gated Teacher-Student Run

Files:

- `teacher_student/schemas.py`
- `teacher_student/prompts.py`
- `teacher_student/verifier.py`
- `teacher_student/service.py`
- `teacher_student/router.py`
- `main.py` router mount only
- `packages/python-sdk/src/decompute_ai/cli.py`
- command docs for `decompute ts-run`, `decompute ts-health`, and `decompute local-chat`

Acceptance:

- feature disabled returns clear disabled response
- `decompute ts-run` calls `/v1/teacher_student/run`
- `decompute ts-health` calls `/v1/teacher_student/health`
- `decompute local-chat` is clearly labeled direct-local debug, not verified teacher-student
- command wrapper sends bounded `source`/`command` metadata
- student-only path works with mock local provider
- student unavailable escalates to teacher when teacher allowed
- verifier hard-fail escalates
- `/v1/messages` regression tests pass unchanged
- normal prompts never invoke the student

### Phase 3: Persistence And Governance

Files:

- `models.py`
- `db.py`
- `alembic/versions/0008_teacher_student.py`
- tests for migration and governance

Acceptance:

- migration upgrades/downgrades
- old rows unaffected
- default metadata-only mode stores no prompt/answer/test content
- route enums are clamped
- usage recording failure cannot 500 the request

### Phase 4: Sandbox And Judge

Files:

- `teacher_student/sandbox.py`
- `teacher_student/judge.py`
- security tests

Acceptance:

- default sandbox disabled
- production rejects unsafe sandbox settings
- container/cloud sandbox has timeout, memory, CPU, PID, filesystem, and network controls
- no bare subprocess in production request path
- judge cannot be same model/provider as student

### Phase 5: Yellowstone Observe Mode

Files:

- `yellowstone/student_capability.py`
- `teacher_student/intent.py`
- `yellowstone/policy.py`
- `yellowstone/planner.py`

Acceptance:

- current dead workload gate is fixed
- observe mode records candidate metadata only
- no local model call on `/v1/messages`
- no latency hit beyond cheap classification

### Phase 6: Local Runtime Lifecycle

Files:

- `packages/python-sdk/src/decompute_ai/cli.py`
- `scripts/start_student_llama_cpp.sh`
- docs

Acceptance:

- `decompute student status/start/stop/install/smoke` works on macOS and Windows where supported
- no automatic weight download
- startup errors are clear

### Phase 7: Distillation Export

Blocked until policy/legal approval.

Deliverables after approval:

- `teacher_student_distillation_records`
- export endpoints
- export scopes
- audit logs
- curation rules
- non-Claude teacher support if Claude outputs remain forbidden

## Test Plan

Unit:

- provider translation
- provider failures
- tool rejection
- context overflow
- verifier deterministic gates
- confidence caps
- eligibility classifier
- Store governance stripping

Integration:

- `/v1/teacher_student/health`
- `decompute ts-run` student accepted
- `decompute ts-run` escalates to teacher
- `decompute ts-run` command wrapper sends bounded `source=cli` and `command=ts-run`
- `/v1/messages` unchanged when local student enabled but no explicit route
- streaming Claude path unchanged

Migration:

- 0008 upgrade/downgrade
- SQLite dev create_all path
- Postgres SQL generated column/index names match ORM

Security:

- mock fallback blocked in production
- unsafe sandbox blocked in production
- metadata-only mode stores no content
- export forbidden unless scoped and enabled
- no weekly beacon content changes
- no API key or Authorization header in route metadata/logs

Manual smoke:

```bash
llama-server -hf yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q4_K_M --port 18080

export DECOMPUTE_LOCAL_STUDENT_ENABLED=true
export DECOMPUTE_LOCAL_STUDENT_BASE_URL=http://127.0.0.1:18080/v1

decompute ts-health
decompute ts-run "Write a Python add(a,b) function." --tests "assert add(2,3)==5"

# Optional direct local-student debug, not verified teacher-student:
decompute local-chat "Draft a Python add(a,b) function."

# Developer diagnostic only. This is not the user-facing V1 surface:
curl -s http://127.0.0.1:8080/v1/teacher_student/run \
  -H 'content-type: application/json' \
  -d '{"task":"Write a Python add(a,b) function.","tests":"assert add(2,3)==5","source":"cli","command":"ts-run"}'
```

## Metrics To Add

Local dashboard only in V1:

- teacher-student cases
- student accepted count
- escalation count
- escalation rate
- teacher called count
- student unavailable count
- average student latency
- average teacher latency after escalation
- estimated teacher cost avoided
- verifier confidence distribution

Do not add content-bearing metrics to telemetry. If counts-only telemetry is added later, use explicit allowlists and tests like the existing privacy beacon.

## Files To Touch

First PR:

- `docs/teacher_student_plan.md`
- `apps/api/decompute_gateway/local_provider.py`
- `apps/api/decompute_gateway/teacher_student/__init__.py`
- `apps/api/decompute_gateway/teacher_student/schemas.py`
- `apps/api/decompute_gateway/teacher_student/prompts.py`
- `apps/api/decompute_gateway/teacher_student/verifier.py`
- `apps/api/decompute_gateway/teacher_student/service.py`
- `apps/api/decompute_gateway/teacher_student/router.py`
- `apps/api/decompute_gateway/config.py`
- `apps/api/decompute_gateway/main.py`
- `packages/python-sdk/src/decompute_ai/cli.py`
- `tests/unit/test_local_provider.py`
- `tests/unit/test_teacher_student_verifier.py`
- `tests/integration/test_teacher_student_route.py`
- `tests/integration/test_teacher_student_command.py`

Second PR:

- `apps/api/decompute_gateway/models.py`
- `apps/api/decompute_gateway/db.py`
- `alembic/versions/0008_teacher_student.py`
- `tests/unit/test_teacher_student_governance.py`
- `tests/unit/test_migrations.py`

Third PR:

- `apps/api/decompute_gateway/yellowstone/student_capability.py`
- `apps/api/decompute_gateway/teacher_student/intent.py`
- `apps/api/decompute_gateway/yellowstone/policy.py`
- `apps/api/decompute_gateway/yellowstone/planner.py`
- `tests/unit/test_student_capability.py`
- `tests/unit/test_yellowstone_student_routes.py`

Fourth PR:

- `scripts/start_student_llama_cpp.sh`
- `docs/teacher_student.md`
- `docs/local_gemma_gguf.md`

## Open Questions

These need answers before later phases, but they do not block Phase 1 or Phase 2:

1. Legal/product: can Claude outputs be stored as traces, and can they ever be used as training labels? Current repo policy says no.
2. Exact V1 student model: confirm the default GGUF, quant, license, expected disk size, and minimum hardware.
3. Hosted runtime: is the student only local laptop, or will hosted/team mode run dedicated GPU workers?
4. Sandbox backend: container/gVisor locally, Cloudflare Sandbox, Firecracker, or another managed runner?
5. Acceptable false-accept rate: what percentage of wrong student answers can ship? The default should target near-zero and accept higher escalation.
6. Judge provider: Claude judge, separate local judge, or no judge in V1?
7. Capture consent: project-level only, or org-level lockout too?

## Release Checklist

- [ ] Existing Claude Code flow manually verified.
- [ ] Existing VS Code extension flow manually verified.
- [ ] OpenClaw `/v1/messages` compatibility still verified.
- [ ] No local model call when feature disabled.
- [ ] No local model call on `/v1/messages` by default.
- [ ] Student unavailable is visible and not mocked.
- [ ] Tool-use requests escalate or skip student.
- [ ] Metadata-only mode stores no prompt/answer/test content.
- [ ] Production config rejects unsafe mock/sandbox/capture settings.
- [ ] Migration is reversible.
- [ ] Dashboard separates compression savings from teacher-student deflection.
- [ ] Weekly telemetry privacy tests still pass.
