"""Coding-intent classifier robustness (UX improvement).

The classifier decides whether the local student is eligible for a request. It used to only recognize
explicit phrasings ("write a Python function to …"), so natural phrasings like "check if a number is
prime in python" silently escalated to Claude. These tests pin that casual coding phrasings (with a
clear coding context) are recognized, while plain prose is NOT — the conservative boundary."""
from __future__ import annotations

import pytest

from decompute_gateway.teacher_student.intent import (
    classify_coding_intent as classify,
    is_student_eligible_intent as eligible,
)


# Casual coding phrasings that MUST be recognized as student-eligible (the UX fix).
ELIGIBLE_CASUAL = [
    "check if a number is prime in python",
    "reverse a linked list in javascript",
    "a bash one-liner to count files in a directory",
    "give me python code that reverses a string",
    "sort a list of dicts by key in python",
    "write a SQL query to find duplicate rows",
    "a regex to match email addresses",
    "parse this json in python",
]

# Explicit phrasings that already worked (regression guard).
ELIGIBLE_EXPLICIT = [
    "Write a Python function to check if a number is prime",
    "implement a binary search function",
    "fix the off-by-one error in this loop",
    "add a docstring to this function",
    "write unit tests for the parser",
]

# Plain prose / non-coding — MUST stay unknown (no false positives onto the student).
NOT_CODING = [
    "write a poem about the ocean",
    "what is the weather today",
    "summarize this article for me",
    "plan my vacation to italy",
    "find me a good restaurant nearby",     # has the verb 'find' but NO coding context
    "reverse the decision we made yesterday",  # 'reverse' but no language/code context
]


@pytest.mark.parametrize("text", ELIGIBLE_CASUAL)
def test_casual_coding_phrasings_are_eligible(text):
    assert eligible(classify(text)), f"{text!r} -> {classify(text)} (expected student-eligible)"


@pytest.mark.parametrize("text", ELIGIBLE_EXPLICIT)
def test_explicit_coding_phrasings_still_eligible(text):
    assert eligible(classify(text)), f"{text!r} -> {classify(text)} (regression)"


@pytest.mark.parametrize("text", NOT_CODING)
def test_non_coding_stays_unknown(text):
    assert not eligible(classify(text)), f"{text!r} -> {classify(text)} (false positive onto student)"


def test_explain_beats_casual_code():
    # "explain what this regex does" is explain_code, not simple_code_patch.
    assert classify("explain what this regex does") == "explain_code"


def test_empty_is_unknown():
    assert classify("") == "unknown"
    assert classify("   ") == "unknown"
