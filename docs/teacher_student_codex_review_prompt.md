# Codex deep-review prompt — Teacher-Student feature

Copy everything below the line into Codex. It is scoped to the teacher-student implementation on
branch `feat/teacher-student-phase0` in `/Users/avii/Documents/Claude-Coder`.

---

You are doing a **deep, adversarial production-readiness review** of a new "teacher-student" feature
in the Decompute gateway (FastAPI, Postgres + SQLAlchemy + Alembic, frozen-dataclass `Settings`).
The feature adds an optional, off-by-default **local coding student** model; Claude stays the teacher
and is called only when a layered verifier escalates. It is command-gated (`/v1/teacher_student/*`)
and must never affect the normal `/v1/messages` proxy path.

Repo: `/Users/avii/Documents/Claude-Coder`, branch `feat/teacher-student-phase0` (built on top of
`main` @ `357bfbe`; the feature is uncommitted working-tree changes + new files).

## Scope — review these files

New:
- `apps/api/decompute_gateway/local_provider.py` (456 lines) — OpenAI-compatible student client
- `apps/api/decompute_gateway/teacher_student/{__init__,schemas,prompts,verifier,service,router,sandbox,sandbox_dev_runner,judge,intent}.py`
- `apps/api/decompute_gateway/yellowstone/student_capability.py` — device/RAM feasibility gate
- `alembic/versions/0008_teacher_student.py` — migration
- `scripts/start_student_llama_cpp.sh`, `scripts/verify_local_student.sh`
- `tests/unit/test_*teacher_student*`, `tests/unit/test_local_provider.py`, `tests/unit/test_student_capability.py`,
  `tests/unit/test_yellowstone_student_routes.py`, `tests/integration/test_teacher_student_route.py`,
  `tests/security/test_proxy_path_has_no_student.py`, `tests/test_cli_teacher_student.py`

Modified:
- `apps/api/decompute_gateway/config.py` (settings + production `validate_for_startup` rules)
- `apps/api/decompute_gateway/db.py` (TS store methods, governance, export)
- `apps/api/decompute_gateway/models.py` (3 TS tables + 4 project consent columns + enum sets)
- `apps/api/decompute_gateway/main.py` (router mount + `/v1/messages` observe-mode hook)
- `apps/api/decompute_gateway/yellowstone/{policy,planner}.py` (fixed the dead routing gate)
- `packages/python-sdk/src/decompute_ai/{cli,client}.py` (ts-run/ts-health/local-chat/ts-export/student commands)
- `CLAUDE.md` (recorded the legal exception; note: this file is gitignored)

The design spec is `docs/teacher_student_plan.md` — treat it as the intended contract and flag any
**divergence** between the plan's invariants and the actual code.

## Non-negotiable invariants to verify (from the plan)

1. `/v1/messages` and `/v1/chat/completions` behave identically by default; normal proxy traffic
   NEVER invokes the student.
2. Local student is opt-in / off by default (`local_student_enabled`).
3. **No silent mock fallback** — a down student returns `student_unavailable` or escalates to the
   teacher, never a canned answer presented as a model response.
4. No tool-use turns reach the student (top-level `tools` OR `tool_use`/`tool_result` blocks).
5. Test execution is never bare `subprocess` on the request path; production requires a real sandbox.
6. Privacy at the storage boundary: default metadata-only stores NO prompt/answer/test content;
   enum-like fields are clamped (mirror the existing `CLIENT_BUCKETS` pattern).
7. Claude outputs are not exported as training labels unless the governed policy flag + sign-off are set.
8. Usage/case recording is best-effort and can never 500 a request.

## Already-identified issues — do NOT just re-report these; assume they will be fixed

Blockers: (1) hardcoded teacher model `"claude-sonnet-4-5"` in `router.py:72`; (2) verifier extracts
only the FIRST code block (`verifier.py:95` `first_code_block`), losing multi-file answers; (3) sandbox
+ syntax check are Python-only (`sandbox.py` `build_python_script`); (4) no concurrency cap on sandbox
containers (DoS risk) while the provider has an `asyncio.Semaphore`; (5) `cloudflare` sandbox backend is
an unimplemented stub, so hosted-prod test execution doesn't exist.

Significant: (6) `estimated_teacher_cost_avoided_usd` computed off student tokens / observed as 0.0;
(7) sandbox image pinned by tag not digest; (8) test timeout not distinguished from failure;
(9) verifier threshold/cap table uncalibrated; (10) `usage_event_id` returned null (case not linked to
`usage_events`).

I want you to find **what we have NOT already found.** Re-confirming the above is fine only if you
spot a sharper/worse instance of it.

## What to hunt for (be adversarial and specific)

- **Concurrency & async correctness:** the module-global health cache and `_semaphores` dict in
  `local_provider.py`; event-loop blocking; the `asyncio.create_subprocess_exec` sandbox under load;
  races between the observe-mode hook and the request lifecycle.
- **Security:** sandbox escape vectors beyond network (filesystem mounts, `--user`, image trust,
  argument injection via model output into the docker argv, tmpfs noexec bypass, resource-limit
  evasion); whether the dev-only bare runner can be reached from the app; auth/scope gaps on the new
  endpoints; SSRF via `local_student_base_url`; prompt/secret leakage into logs, route metadata, or
  exceptions; the export path leaking content despite governance mode.
- **Privacy/governance correctness:** does `effective_capture_mode` + `record_teacher_student_case`
  truly drop content in every non-`redacted` mode? Is `prompt_sha256` ever computed over PII without
  scrubbing? Can the export endpoint ever emit Claude outputs without the sign-off? Retention/`expires_at`
  correctness across SQLite (naive) vs Postgres (aware) datetimes.
- **Data model / migration:** 0008 up/down round-trip on BOTH SQLite and Postgres; index/constraint
  name collisions; the `create_all` local-dev path vs alembic drift; nullability/default mismatches
  between the ORM and the migration; FK `ondelete` behavior.
- **Verifier soundness:** ways a wrong answer is accepted (cap math, the `first_code_block` gap,
  truncation/refusal regexes that miss real cases, the judge JSON parser, language detection); ways a
  correct answer is needlessly escalated; whether `student-grades-self` can ever happen via the judge.
- **Failure modes:** student returns 200 with an error body; partial/streaming responses; oversized
  outputs; teacher call failure handling; what happens when `store` is None; uncaught exceptions that
  could 500 a `/v1/teacher_student/run`.
- **Config validation gaps:** combinations `validate_for_startup` should reject but doesn't (e.g.
  capture enabled in prod with metadata-only-but-no-redaction, non-loopback https, judge == student
  model, sandbox runtime that isn't on PATH).
- **Plan divergence:** anything the code does that contradicts `docs/teacher_student_plan.md`, and any
  plan requirement that is silently unimplemented or stubbed.
- **Tests:** assertions that pass for the wrong reason, missing negative/edge cases, over-mocking that
  hides real behavior, and any invariant above that has NO test.

## Output format

For each finding: **severity** (blocker/high/medium/low), **file:line**, a one-paragraph explanation of
the concrete failure (with a trigger/repro if possible), and a **specific** suggested fix. Group by
severity, most severe first. Call out explicitly any of the 8 invariants you can defeat. End with a
short list of the **highest-leverage** changes if only 3 things could be fixed before a flagged V1 ship.

Do not rewrite the code; produce the review.
