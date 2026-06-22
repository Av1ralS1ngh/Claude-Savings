"""Layered, escalation-biased verifier for student answers (plan §"Verifier Design").

A confident-looking wrong answer must NOT pass just because it has a code fence. The verifier
combines independent signals and is biased to escalate to the teacher when evidence is missing:

  Layer 0  Deterministic checks   — always runs; no network/model/sandbox.
  Layer 1  Objective tests        — only when tests + a real sandbox are available (Phase 4).
  Layer 2  Judge                  — optional, default-off; an independent model finds flaws (Phase 4).
  Layer 3  Calibrated threshold   — confidence caps by available evidence; accept only above threshold.

Decision rule (plan):
  accept student only if no hard-risk layer fired AND confidence >= threshold AND confidence was
  not capped below threshold by missing evidence; otherwise escalate.

The student NEVER grades itself. Layers 1 and 2 are injected by the service (`run_tests_fn`,
`judge_fn`); when absent, their confidence cap applies, which is what makes "no tests, no judge"
almost always escalate — intentional until calibration data exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ast
import re
from typing import Awaitable, Callable, Protocol

VERIFIER_VERSION = "v1"

# Confidence caps by available evidence (plan §"Layer 3"). The final confidence is the MIN of the
# base score and the applicable cap, so missing evidence pulls a confident-looking answer below the
# threshold and forces escalation.
CAP_TESTS_PASSED = 1.00      # tests ran in sandbox and passed — strongest evidence
# An independent, skeptical Claude judge passing (find-flaws prompt, fails closed on doubt — see
# judge.py) is strong evidence on its own: it is the primary serve signal on the /v1/messages proxy
# path, where requests carry no user tests. Set to base_confidence so a judge-approved answer with no
# deterministic flaws reaches the base score and can clear a threshold up to that value. Keep STRICTLY
# below CAP_TESTS_PASSED so executed-and-passing tests always outrank a judge opinion.
CAP_JUDGE_PASSED = 0.85      # no tests but an independent judge passed
CAP_SANDBOX_UNAVAILABLE = 0.60  # tests provided but no sandbox to run them
CAP_NO_EVIDENCE = 0.55       # no tests and no judge — always escalates

_REFUSAL_RE = re.compile(
    r"\b(i can't|i cannot|i'm sorry|i am sorry|as an ai|i'm unable|i am unable|cannot help with)\b",
    re.I,
)
_CODE_FENCE_RE = re.compile(r"```([\w+-]*)\n(.*?)```", re.S)
_PY_FENCE_RE = re.compile(r"```(?:python|py)\b", re.I)
# Languages we can syntax-check / run. A non-Python coding answer is detected but NOT executed
# (Orig-3): the verifier caps confidence and escalates rather than mis-running it.
_PY_LANGS = {"", "python", "py", "python3"}


@dataclass
class Layer:
    name: str
    passed: bool
    detail: str | None = None
    severity: int = 0  # 0 = ok/soft, >=2 = hard risk (forces escalate)


@dataclass
class VerifierResult:
    decision: str  # "accept_student" | "escalate"
    confidence: float
    threshold: float
    escalation_reason: str | None
    layers: list[Layer] = field(default_factory=list)
    tests_ran: bool = False
    tests_passed: bool | None = None
    sandbox_backend: str = "disabled"
    judge_model: str | None = None

    @property
    def escalated(self) -> bool:
        return self.decision == "escalate"


class TestRunner(Protocol):
    async def __call__(self, *, code: str, tests: str) -> "TestRunResult": ...


@dataclass
class TestRunResult:
    ran: bool
    passed: bool
    backend: str
    detail: str | None = None


@dataclass
class JudgeResult:
    passed: bool
    model: str
    severity: int = 0
    detail: str | None = None


JudgeFn = Callable[..., Awaitable[JudgeResult]]


# ── Layer 0: deterministic ─────────────────────────────────────────────────────


def first_code_block(text: str) -> str | None:
    m = _CODE_FENCE_RE.search(text or "")
    return m.group(2) if m else None


def all_code_blocks(text: str) -> list[tuple[str, str]]:
    """Return [(lang, body), ...] for every fenced block, in order."""
    return [(lang.lower(), body) for lang, body in _CODE_FENCE_RE.findall(text or "")]


def runnable_python_code(text: str) -> str | None:
    """Concatenate ALL Python (or unlabeled) code blocks so a multi-file answer is tested as a whole
    (Orig-2). Returns None if there is no code, or if the answer contains a NON-Python labeled block
    (which we can't run safely — Orig-3), so such answers escalate instead of being mis-run."""
    blocks = all_code_blocks(text)
    if not blocks:
        return None
    if any(lang not in _PY_LANGS for lang, _ in blocks):
        return None  # a non-Python block present -> not safely runnable here
    return "\n\n".join(body for _, body in blocks)


def _looks_like_coding_task(task_text: str) -> bool:
    t = (task_text or "").lower()
    return any(k in t for k in ("function", "implement", "code", "class ", "def ", "write a", "fix", "bug", "test", "script", "program", "method"))


def _looks_truncated(text: str) -> bool:
    s = (text or "").rstrip()
    if not s:
        return False
    # Unbalanced fences, or an obvious mid-token cut.
    if s.count("```") % 2 == 1:
        return True
    return s.endswith(("def ", "return", "=", "(", ",", "\\"))


def deterministic_layers(*, task_text: str, answer: str, tests: str | None) -> list[Layer]:
    """Layer 0 — pure structural checks. Hard-risk layers (severity>=2) force escalation."""
    layers: list[Layer] = []
    text = answer or ""

    if not text.strip():
        layers.append(Layer("empty_answer", passed=False, detail="student returned no content", severity=3))
        return layers  # nothing else is meaningful

    if _REFUSAL_RE.search(text):
        layers.append(Layer("refusal", passed=False, detail="answer contains refusal language", severity=3))

    # Truncation first: an answer with an opening ``` but no closing fence is a cut-off generation,
    # which is a more accurate (and more actionable) signal than "missing code".
    truncated = _looks_truncated(text)
    if truncated:
        layers.append(Layer("truncated", passed=False, detail="answer appears truncated", severity=2))

    coding = _looks_like_coding_task(task_text)
    code = first_code_block(text)
    if coding and code is None and not truncated:
        layers.append(Layer("missing_code", passed=False, detail="coding task but no code block in answer", severity=2))
    elif code is None and not truncated:
        layers.append(Layer("no_code_fence", passed=True, detail="no code fence (non-coding task)", severity=1))

    # Syntax check Python answers (the only language we can parse cheaply without deps).
    if code is not None and _PY_FENCE_RE.search(text):
        try:
            ast.parse(code)
            layers.append(Layer("py_syntax", passed=True))
        except SyntaxError as exc:
            layers.append(Layer("py_syntax", passed=False, detail=f"python syntax error: {exc.msg}", severity=2))

    if tests is None:
        layers.append(Layer("no_tests", passed=True, detail="no tests supplied", severity=1))

    return layers


# ── Orchestration ──────────────────────────────────────────────────────────────


async def verify(
    *,
    task_text: str,
    answer: str,
    tests: str | None,
    threshold: float,
    base_confidence: float = 0.85,
    run_tests_fn: TestRunner | None = None,
    judge_fn: JudgeFn | None = None,
    sandbox_backend: str = "disabled",
) -> VerifierResult:
    """Run all available layers and decide. `run_tests_fn`/`judge_fn` are injected by the service;
    when None, their confidence cap applies (which is what makes missing-evidence escalate)."""
    layers = deterministic_layers(task_text=task_text, answer=answer, tests=tests)

    hard_fail = next((layer for layer in layers if layer.severity >= 2 and not layer.passed), None)
    if hard_fail is not None:
        # No point spending a sandbox/judge on a structurally broken answer.
        return VerifierResult(
            decision="escalate",
            confidence=0.0,
            threshold=threshold,
            escalation_reason=hard_fail.name,
            layers=layers,
            sandbox_backend=sandbox_backend,
        )

    confidence = base_confidence
    tests_ran = False
    tests_passed: bool | None = None
    judge_model: str | None = None
    cap = CAP_NO_EVIDENCE

    # Layer 1: objective tests. Use ALL python blocks concatenated (Orig-2); None if a non-python
    # block makes the answer unrunnable here (Orig-3) -> falls through to the unavailable cap.
    code = runnable_python_code(answer)
    if tests:
        if run_tests_fn is not None and code is not None:
            try:
                tr = await run_tests_fn(code=code, tests=tests)
            except Exception as exc:
                # A sandbox-runner crash must NOT 500 the request (H4): record it and treat tests as
                # unavailable so confidence is capped and the answer escalates.
                tr = TestRunResult(ran=False, passed=False, backend="error", detail=f"runner error: {type(exc).__name__}")
            tests_ran = tr.ran
            if tr.ran:
                tests_passed = tr.passed
                layers.append(Layer("objective_tests", passed=tr.passed, detail=tr.detail, severity=0 if tr.passed else 3))
                if not tr.passed:
                    return VerifierResult(
                        decision="escalate", confidence=0.0, threshold=threshold,
                        escalation_reason="tests_failed", layers=layers,
                        tests_ran=True, tests_passed=False, sandbox_backend=tr.backend,
                    )
                cap = CAP_TESTS_PASSED
                sandbox_backend = tr.backend
            else:
                # Could not run safely — do NOT pretend they passed.
                layers.append(Layer("objective_tests", passed=True, detail="tests not run (sandbox unavailable)", severity=1))
                cap = CAP_SANDBOX_UNAVAILABLE
        else:
            # Tests provided but execution not enabled/available.
            layers.append(Layer("objective_tests", passed=True, detail="tests provided but execution disabled", severity=1))
            cap = CAP_SANDBOX_UNAVAILABLE

    # Layer 2: judge (only consulted when tests didn't already give us a 1.0 cap).
    if judge_fn is not None and cap < CAP_TESTS_PASSED:
        try:
            jr = await judge_fn(task_text=task_text, answer=answer, tests=tests)
        except Exception as exc:
            # Judge unavailable/crashed is an explicit outcome, not a request crash (H4 + plan
            # "failure is loud"). Record it and escalate — never silently accept.
            layers.append(Layer("judge", passed=False, detail=f"judge unavailable: {type(exc).__name__}", severity=2))
            return VerifierResult(
                decision="escalate", confidence=0.0, threshold=threshold,
                escalation_reason="judge_unavailable", layers=layers,
                tests_ran=tests_ran, tests_passed=tests_passed, sandbox_backend=sandbox_backend,
            )
        judge_model = jr.model
        layers.append(Layer("judge", passed=jr.passed, detail=jr.detail, severity=jr.severity if not jr.passed else 0))
        if not jr.passed or jr.severity >= 2:
            return VerifierResult(
                decision="escalate", confidence=0.0, threshold=threshold,
                escalation_reason="judge_flagged", layers=layers,
                tests_ran=tests_ran, tests_passed=tests_passed,
                sandbox_backend=sandbox_backend, judge_model=judge_model,
            )
        if cap < CAP_JUDGE_PASSED:
            cap = CAP_JUDGE_PASSED

    # Layer 3: calibrated threshold with the evidence cap applied.
    final_confidence = min(confidence, cap)
    if final_confidence >= threshold:
        decision, reason = "accept_student", None
    else:
        decision = "escalate"
        # Distinguish "capped by missing evidence" from a genuinely low base score.
        reason = "low_confidence_no_evidence" if cap < threshold else "below_threshold"

    return VerifierResult(
        decision=decision,
        confidence=round(final_confidence, 4),
        threshold=threshold,
        escalation_reason=reason,
        layers=layers,
        tests_ran=tests_ran,
        tests_passed=tests_passed,
        sandbox_backend=sandbox_backend,
        judge_model=judge_model,
    )
