"""Command-gated teacher-student coding flow.

The local coding student answers narrow coding tasks; Claude stays the teacher and is called only
to correct/answer when the layered verifier escalates. This package is reachable ONLY through the
explicit `/v1/teacher_student/*` plumbing (driven by `decompute ts-run`/`ts-health`/`local-chat`),
never from the default `/v1/messages` proxy path. See docs/teacher_student_plan.md.
"""
from __future__ import annotations

from .router import include_teacher_student_router

__all__ = ["include_teacher_student_router"]
