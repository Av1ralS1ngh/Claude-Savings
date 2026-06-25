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
# A language name is a strong signal the user wants code, even in casual phrasing
# ("check if a number is prime in python", "a bash one-liner to ...").
_LANG_RE = re.compile(
    r"\b(python|javascript|typescript|java|golang|go lang|rust|c\+\+|c#|ruby|php|bash|shell|sql|"
    r"kotlin|swift|scala|haskell|html|css|regex|json|yaml)\b",
    re.I,
)
# Casual coding-task signals that don't require a "function/class" noun. Three forms:
#   1. an explicit code noun ("snippet", "one-liner", "script", "regex", "query", "command")
#   2. a produce-verb + a connector ("write a … to …", "give me … that …")
#   3. a common algorithmic verb ("reverse", "sort", "check if", "compute", "parse", "count" …)
# All require a coding CONTEXT (a language or code hint) to count — see classify_coding_intent — so
# plain prose like "write a poem" or "summarize this article" stays unknown.
_CASUAL_CODE_RE = re.compile(
    r"\b(code|snippet|one[- ]?liner|script|program|regex|query|command)\b"
    r"|\b(write|create|generate|give me|show me|produce)\b[^.\n]{0,40}\b(to|that|which|for)\b"
    r"|\b(reverse|sort|compute|calculate|parse|count|check if|validate|convert|encode|decode|"
    r"merge|filter|search|find|return|print|read|format|split|join|remove|replace|append)\b",
    re.I,
)


def classify_coding_intent(text: str) -> str:
    """Return the coding intent of a request's text. Conservative: `unknown` when unsure.

    Recognizes both explicit phrasings ("write a function to …") and casual ones ("check if a number
    is prime in python", "a bash one-liner to …") — but a coding CONTEXT (a language name or code-ish
    hint) is required for the looser patterns, so plain prose never misclassifies as code."""
    t = (text or "").strip()
    if not t:
        return "unknown"
    has_code_context = bool(_LANG_RE.search(t) or _CODE_HINT_RE.search(t))
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
    # "Explain/what does this code do" is a distinct, more-specific intent than a casual code request;
    # check it BEFORE the loose casual-code pattern (which would otherwise grab "explain what this
    # regex does").
    if _EXPLAIN_RE.search(t):
        return "explain_code"
    # Casual phrasing, but only when there's a clear coding context (language/code hint) so we don't
    # grab plain prose. Bounded length keeps it to short, student-sized tasks.
    if has_code_context and _CASUAL_CODE_RE.search(t) and len(t) < 1000:
        return "simple_code_patch"
    return "unknown"


def is_student_eligible_intent(intent: str) -> bool:
    return intent in STUDENT_ELIGIBLE_INTENTS
