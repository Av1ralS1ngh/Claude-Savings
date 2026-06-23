"""Structural guard for plan invariant #11: the proxy path never reaches the student UNCONDITIONALLY.

This is a *source-level* assertion (not just a runtime sys.modules check) so it holds regardless
of import timing: the teacher-student feature stays isolated, and every coupling from main.py is an
explicitly enumerated, reviewed line. The sanctioned couplings are:
  * the command-gated router mount (`include_teacher_student_router(...)`, Phase 2);
  * the OBSERVE-mode regex intent classifier (analytics only, cannot serve);
  * the OPT-IN inline serve path (`_try_serve_with_student`, gated behind `local_student_serve`):
    it attempts the student only when that flag is on, serves only on a verifier `student_only`
    accept, and on anything else returns None so /v1/messages falls through to the normal Claude
    call — the student can ADD a local fast-path but never replace or break the request.
Any reference OUTSIDE this set — importing local_provider, an unconditional student call, etc. — is
forbidden and trips this test.

The forbidden token `local_provider` still NEVER appears in main.py: the serve path goes through the
service (which owns the provider), so the proxy never imports the model-calling module directly.

If you add a new sanctioned coupling, extend SANCTIONED_SUBSTRINGS with the *exact* lines and explain
why here — do not delete or broaden the test.
"""
from __future__ import annotations

import pathlib

GATEWAY = pathlib.Path(__file__).resolve().parents[2] / "apps" / "api" / "decompute_gateway"

# References to the teacher-student feature that are allowed to appear in main.py. Phase 2 adds the
# router-mount import + call; until then this set is empty and main.py must mention none of it.
SANCTIONED_SUBSTRINGS: set[str] = {
    "include_teacher_student_router",  # the router mount call + its import
    "from .teacher_student.router import",  # also covers importing build_teacher_student_service
    # OBSERVE-mode analytics only: a pure, side-effect-free coding-intent classifier (regex). It
    # CANNOT serve the student — it has no provider, no network, no model. The proxy uses it to record
    # a "would have tried the student" candidate in route metadata (plan §"Yellowstone And Routing").
    "from .teacher_student.intent import classify_coding_intent",
    "classify_coding_intent(",
    # Startup maintenance: purge expired captured TS content (retention enforcement, H2). A store
    # method call — it does not serve the student, it deletes stale rows.
    "purge_expired_teacher_student_cases(",
    # OPT-IN inline serve path (DECOMPUTE_LOCAL_STUDENT_SERVE, off by default). _try_serve_with_student
    # builds the shared service and runs one verified attempt; it serves ONLY a student_only accept and
    # otherwise returns None to fall through to Claude. Gated + fail-open by construction (see its body
    # + tests/integration/test_student_serve_path.py).
    "from .teacher_student.schemas import TeacherStudentRunRequest",
    "build_teacher_student_service(",
    "TeacherStudentRunRequest(",
}

# `local_provider` is the module that actually CALLS the student model — it must NEVER appear in the
# proxy path, with no exceptions. `teacher_student` references are allowed only via SANCTIONED lines.
FORBIDDEN_TOKENS = ("local_provider", "teacher_student", "LocalProvider", "TeacherStudent")


def _offending_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not any(tok in line for tok in FORBIDDEN_TOKENS):
            continue
        if any(ok in line for ok in SANCTIONED_SUBSTRINGS):
            continue
        out.append((i, stripped))
    return out


def test_main_does_not_couple_to_student_outside_router_mount():
    main_py = GATEWAY / "main.py"
    offending = _offending_lines(main_py)
    assert offending == [], (
        "main.py references the teacher-student feature outside the sanctioned router mount:\n"
        + "\n".join(f"  L{n}: {s}" for n, s in offending)
    )
