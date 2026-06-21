"""Independent flaw-finding judge (plan §"Layer 2").

An optional, default-off verifier layer. A separate model is asked to FIND FLAWS in the student's
answer (not to praise it); a `fail`/`uncertain` verdict, severity >= 2, or "would not ship"
escalates. Hard rule: the judge can NEVER be the same model/provider as the student — a model
grading itself is not an independent signal (plan principle #3, invariant on student-grades-self).

The judge reuses the gateway's one Anthropic teacher path (injected `teacher_caller`), so by
construction it is Claude, not the local student. `make_judge(settings, teacher_caller)` returns a
`judge_fn` for verifier.verify, or None when judging is disabled or no distinct judge is available.
"""
from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from ..config import Settings
from .verifier import JudgeResult

JUDGE_SYSTEM_PROMPT = (
    "You are a strict code reviewer. Your job is to FIND FLAWS in a junior's coding answer, not to "
    "praise it. Consider correctness, edge cases, security, and whether you would ship it. "
    "Respond ONLY with a compact JSON object: "
    '{"verdict": "pass" | "fail" | "uncertain", "severity": 0-3, "would_ship": true|false, '
    '"reason": "<one sentence>"}. Use fail/uncertain when in doubt — a wrong answer that looks '
    "confident is worse than escalating."
)

# A teacher caller takes (messages, system) and returns an object with `.text`.
JudgeTeacherCaller = Callable[..., Awaitable[Any]]

_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_judge_text(text: str) -> JudgeResult:
    """Parse the judge model's JSON reply into a JudgeResult, defaulting to ESCALATE on any doubt."""
    m = _JSON_RE.search(text or "")
    if not m:
        # Unparseable -> treat as uncertain (escalate). Never silently pass.
        return JudgeResult(passed=False, model="judge", severity=2, detail="unparseable judge reply")
    try:
        data = json.loads(m.group(0))
    except Exception:
        return JudgeResult(passed=False, model="judge", severity=2, detail="invalid judge JSON")
    verdict = str(data.get("verdict", "uncertain")).lower()
    severity = _coerce_severity(data.get("severity", 0))
    would_ship = bool(data.get("would_ship", False))
    reason = str(data.get("reason", ""))[:300]
    passed = verdict == "pass" and would_ship and severity < 2
    return JudgeResult(passed=passed, model="judge", severity=severity, detail=reason or verdict)


def _coerce_severity(value: Any) -> int:
    """Severity may arrive as an int or a word ('low'/'medium'/'high'/'critical'). Never raise; an
    unparseable value is treated as high (escalate)."""
    if isinstance(value, bool):
        return 2 if value else 0
    if isinstance(value, (int, float)):
        return max(0, min(3, int(value)))
    words = {"none": 0, "low": 1, "medium": 2, "med": 2, "moderate": 2, "high": 3, "critical": 3, "severe": 3}
    return words.get(str(value).strip().lower(), 2)


def make_judge(settings: Settings, teacher_caller: JudgeTeacherCaller | None):
    """Return a `judge_fn` for verifier.verify, or None if judging is disabled / unavailable.

    The judge is the Claude teacher path, which is by construction a different provider than the local
    student — satisfying the no-self-judge rule."""
    if not getattr(settings, "teacher_student_judge_enabled", False):
        return None
    if teacher_caller is None:
        return None

    async def judge_fn(*, task_text: str, answer: str, tests: str | None) -> JudgeResult:
        user = [{
            "role": "user",
            "content": (
                f"Task:\n{task_text}\n\n"
                + (f"Tests:\n{tests}\n\n" if tests else "")
                + f"Junior's answer:\n{answer}\n\nReview it."
            ),
        }]
        result = await teacher_caller(messages=user, system=JUDGE_SYSTEM_PROMPT)
        return parse_judge_text(getattr(result, "text", "") or "")

    return judge_fn


def assert_judge_distinct_from_student(*, judge_model: str | None, student_model: str) -> None:
    """Guard used in tests/config: a judge must never be the same model as the student."""
    if judge_model and judge_model.strip().lower() == student_model.strip().lower():
        raise ValueError("judge model must differ from the student model (no self-judging)")
