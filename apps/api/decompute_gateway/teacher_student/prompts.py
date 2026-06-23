"""Prompt templates for the teacher-student flow.

Kept small and deterministic. The student prompt steers a local coder toward correct, runnable code
with a code fence; the teacher-correction prompt asks Claude to fix/redo the task given the student's
attempt and the escalation reason, so the teacher answer is a genuine correction, not a fresh start
that ignores what the student produced.
"""
from __future__ import annotations

STUDENT_SYSTEM_PROMPT = (
    "You are a focused coding assistant. Solve the task directly and correctly.\n"
    "- Return complete, runnable code in a single fenced code block for the primary file.\n"
    "- Prefer standard-library solutions; do not invent dependencies.\n"
    "- If tests are provided, make the code pass them.\n"
    "- Do not ask clarifying questions; make the most reasonable assumption and state it briefly.\n"
    "- Do not include tool calls, shell commands to run, or repository edits — only the code and a short explanation."
)


def teacher_correction_system_prompt() -> str:
    return (
        "You are a senior engineer reviewing and correcting a junior's coding attempt. "
        "Produce the correct, complete solution. If the attempt is salvageable, fix it; otherwise "
        "rewrite it. Return runnable code in a fenced block and a one-line summary of what was wrong."
    )


def build_teacher_correction_messages(
    *,
    task_text: str,
    student_answer: str,
    escalation_reason: str | None,
    tests: str | None,
) -> list[dict[str, str]]:
    """Build the user turn for the teacher correction call. The system prompt is supplied separately
    via the Anthropic `system` field by the service."""
    parts = [f"Task:\n{task_text}"]
    if tests:
        parts.append(f"\nTests the solution must satisfy:\n{tests}")
    parts.append(f"\nThe local student produced this attempt:\n{student_answer}")
    if escalation_reason:
        parts.append(f"\nThe automated verifier escalated for this reason: {escalation_reason}")
    parts.append("\nReturn the corrected, complete solution.")
    return [{"role": "user", "content": "\n".join(parts)}]
