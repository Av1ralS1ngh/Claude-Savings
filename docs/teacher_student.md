# Teacher-Student Coding Flow

An optional, **off-by-default** local coding *student* bolted onto the Decompute gateway. The student
answers narrow coding tasks locally; Claude stays the *teacher* and is called only when the verifier
escalates. It is reachable **only** through explicit commands — normal Claude Code / OpenClaw / SDK
traffic through `/v1/messages` is never affected.

See the build spec in [`teacher_student_plan.md`](./teacher_student_plan.md).

## Quick start

```bash
# 1. Start a local model (user-managed; the gateway never downloads weights or starts this for you).
#    Pick a model that FITS your RAM — see local_gemma_gguf.md. On 16 GB, do NOT use a 12B.
bash scripts/start_student_llama_cpp.sh   # or: decompute student start --model <repo:quant>

# 2. Point the gateway at it and enable the feature.
export DECOMPUTE_LOCAL_STUDENT_ENABLED=true
export DECOMPUTE_LOCAL_STUDENT_BASE_URL=http://127.0.0.1:18080/v1

# 3. Use it.
decompute ts-health
decompute ts-run "Write a Python add(a, b) function." --tests "assert add(2,3)==5"
decompute local-chat "Draft a Python add(a, b) function."   # direct, UNVERIFIED debug
```

## Commands

| Command | What it does |
|---|---|
| `decompute ts-run "<task>"` | One verified teacher-student run (student → verify → escalate to Claude if risky). |
| `decompute ts-run … --tests <inline-or-path>` | Provide tests the solution must satisfy. |
| `decompute ts-run … --require-teacher` | Always get Claude's review/correction. |
| `decompute ts-run … --execute-tests` | Run tests in the sandbox (only if server policy allows). |
| `decompute ts-health` | Show student/teacher/verifier/sandbox/governance readiness. |
| `decompute local-chat "<prompt>"` | Direct call to the local student — **unverified**, debug only. |
| `decompute student status/start/stop/smoke/install/pull` | Manage the local model runtime. |

## How a run is decided (the verifier)

The verifier is **escalation-biased** — a confident-looking wrong answer must not pass just because
it has a code fence. Layers (plan §"Verifier Design"):

1. **Deterministic** (always): empty/refusal/missing-code/syntax-error/truncated → hard escalate.
2. **Objective tests** (only with tests + a real sandbox): tests fail → escalate; can't run → cap confidence.
3. **Judge** (optional, default-off): an independent model finds flaws; flagged → escalate.
4. **Calibrated threshold** with evidence caps:

   | Evidence | Confidence cap |
   |---|---|
   | tests passed in sandbox | 1.00 |
   | judge passed, no tests | 0.80 |
   | tests provided but no sandbox | 0.60 |
   | no tests, no judge | 0.55 |

   The default threshold is `0.82`, so **"no tests, no judge" almost always escalates** — intentional
   until calibration data exists.

## Routes

`student_only` · `teacher_corrected` · `teacher_only` · `student_unavailable` · `disabled`.
A down student is **loud** (`student_unavailable` or escalation to the teacher) — never a mock answer.

## Privacy & governance

- Default storage is **metadata-only**: no prompts, answers, or tests are stored — only hashes,
  token counts, features, and verdict/route metadata.
- Content capture is opt-in and requires **both** server config (`DECOMPUTE_TS_CAPTURE_ENABLED`) and
  project config; `capture_locked` forces metadata-only regardless.
- Export of training data needs dedicated scopes (`distill:read` / `distill:admin`), never implied by
  `messages:write`. Distillation on Claude outputs is approved but governed — see `CLAUDE.md`.

## Key settings (`DECOMPUTE_*`)

| Setting | Default | Notes |
|---|---|---|
| `DECOMPUTE_LOCAL_STUDENT_ENABLED` | `false` | master switch |
| `DECOMPUTE_LOCAL_STUDENT_BASE_URL` | `http://127.0.0.1:18080/v1` | OpenAI-compatible endpoint |
| `DECOMPUTE_LOCAL_STUDENT_MODEL` | `local-student-coder` | model id the backend reports |
| `DECOMPUTE_TS_CONFIDENCE_THRESHOLD` | `0.82` | verifier accept threshold |
| `DECOMPUTE_TS_JUDGE_ENABLED` | `false` | enable the independent judge |
| `DECOMPUTE_TS_TEST_EXECUTION` | `false` | run tests in a sandbox |
| `DECOMPUTE_TS_SANDBOX_BACKEND` | `disabled` | `container` (docker/podman) or `cloudflare` |
| `DECOMPUTE_TS_CAPTURE_ENABLED` | `false` | server-level capture gate |

See [`.env.example`](../.env.example) for the full list.
