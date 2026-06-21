"""Coding-intent classifier for teacher-student eligibility (plan §"Yellowstone And Routing").

The repo's workload.py classifies a request by the *content type* of its bulk (structured_json/html/
logs/source_code/prose/short/mixed) for savings analytics. That is the wrong axis for deciding
whether the local student should attempt a request — and it was the source of the dead gate in
policy.py, which checked classes workload.py never emits.

This classifier answers a different question: "is this a narrow coding task the student could plausibly
handle?" It emits the small allowlist the policy actually wants:

  simple_code_patch | lint_fix | docs_edit | test_generation | explain_code | unknown

It is cheap, deterministic, and intentionally conservative: when unsure it returns `unknown`, which
makes the request ineligible for the student.
"""
from __future__ import annotations

import re

CODING_INTENTS = frozenset({
    "simple_code_patch", "lint_fix", "docs_edit", "test_generation", "explain_code", "unknown",
})

# Intents the student is allowed to attempt (explain_code is excluded by default — it is low-risk but
# also low-value to deflect, and policy can opt it in later).
STUDENT_ELIGIBLE_INTENTS = frozenset({"simple_code_patch", "lint_fix", "docs_edit", "test_generation"})

_TEST_RE = re.compile(r"\b(write|add|generate|create)\b.{0,40}\b(unit )?tests?\b|\bpytest\b|\bassert\b", re.I)
_LINT_RE = re.compile(r"\b(lint|flake8|ruff|eslint|format|prettier|black|type[- ]?check|mypy|style)\b", re.I)
_DOCS_RE = re.compile(r"\b(docstring|documentation|readme|comment|explain in the docs|jsdoc)\b", re.I)
_EXPLAIN_RE = re.compile(r"\b(explain|what does (this|the)|how does (this|it)|walk me through|summari[sz]e (this|the) code)\b", re.I)
# Verbs that signal "produce/modify code": fix/patch/refactor/etc., OR write/create/implement/add a
# function|class|method|script|program (the most common phrasing of a coding task).
_PATCH_RE = re.compile(
    r"\b(fix|patch|implement|refactor|correct|rename|debug)\b"
    r"|\b(write|create|implement|add|build|make)\b[^.\n]{0,60}\b(function|class|method|script|program|module|endpoint|handler|parser|algorithm)\b",
    re.I,
)
_CODE_HINT_RE = re.compile(r"```|\bdef \b|\bclass \b|\bfunction\b|\bimport \b", re.I)


def classify_coding_intent(text: str) -> str:
    """Return the coding intent of a request's text. Conservative: `unknown` when unsure."""
    t = (text or "").strip()
    if not t:
        return "unknown"
    # Most specific first.
    if _TEST_RE.search(t):
        return "test_generation"
    if _LINT_RE.search(t):
        return "lint_fix"
    if _DOCS_RE.search(t):
        return "docs_edit"
    # A patch-y verb + some code-ish signal -> simple code patch.
    if _PATCH_RE.search(t) and (_CODE_HINT_RE.search(t) or len(t) < 1000):
        return "simple_code_patch"
    if _EXPLAIN_RE.search(t):
        return "explain_code"
    return "unknown"


def is_student_eligible_intent(intent: str) -> bool:
    return intent in STUDENT_ELIGIBLE_INTENTS
