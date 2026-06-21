"""Structural guard for plan invariant #11: the default proxy path never reaches the student.

This is a *source-level* assertion (not just a runtime sys.modules check) so it holds regardless
of import timing: the teacher-student feature must stay isolated behind its own command-gated
router. The only sanctioned coupling from main.py is a single router-mount line of the form
`include_teacher_student_router(...)` (added in Phase 2, mirroring include_support_bundle_router).
Any other reference — importing local_provider, calling the student service from the /v1/messages
or /v1/chat/completions handlers — is forbidden and trips this test.

If you are adding the Phase 2 router mount, extend SANCTIONED_SUBSTRINGS, do not delete the test.
"""
from __future__ import annotations

import pathlib

GATEWAY = pathlib.Path(__file__).resolve().parents[2] / "apps" / "api" / "decompute_gateway"

# References to the teacher-student feature that are allowed to appear in main.py. Phase 2 adds the
# router-mount import + call; until then this set is empty and main.py must mention none of it.
SANCTIONED_SUBSTRINGS: set[str] = {
    "include_teacher_student_router",  # the router mount call + its import
    "from .teacher_student.router import",
    # OBSERVE-mode analytics only: a pure, side-effect-free coding-intent classifier (regex). It
    # CANNOT serve the student — it has no provider, no network, no model. The proxy uses it to record
    # a "would have tried the student" candidate in route metadata (plan §"Yellowstone And Routing").
    "from .teacher_student.intent import classify_coding_intent",
    "classify_coding_intent(",
    # Startup maintenance: purge expired captured TS content (retention enforcement, H2). A store
    # method call — it does not serve the student, it deletes stale rows.
    "purge_expired_teacher_student_cases(",
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
