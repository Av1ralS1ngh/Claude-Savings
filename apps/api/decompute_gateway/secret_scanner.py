from __future__ import annotations

from dataclasses import dataclass, asdict
import re


@dataclass(frozen=True)
class SecretFinding:
    kind: str
    start: int
    end: int
    masked: str  # never stores reconstructable secret material

    def to_dict(self) -> dict:
        return asdict(self)


# (kind, pattern, is_pii). Credentials AND user PII both switch CCR into a
# non-storing / redacted mode so neither is ever persisted in our store.
_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), False),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{32,}"), False),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"), False),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}"), False),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}"), False),
    ("slack_token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"), False),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), False),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), False),
    ("env_secret_assignment", re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd)\s*[=:]\s*['\"]?[^\s'\"]{6,}"), False),
    ("database_url_with_password", re.compile(r"(?i)(?:postgres|mysql|mongodb)(?:ql)?://[^\s:/]+:[^\s@]+@"), False),
    # ── user PII ──
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), True),
    # Phone/card require real separators so bare ID/line-number digit runs in JSON
    # don't false-positive (which would needlessly suppress compression).
    ("phone", re.compile(r"(?<![\d.])(?:\+\d{1,3}[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\d.])"), True),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), True),
    ("credit_card", re.compile(r"\b\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4}\b"), True),
)


def _mask(kind: str, raw: str) -> str:
    """A reference that reveals the *kind* and length but no secret material."""
    return f"<{kind}:{len(raw)}chars>"


class SecretScanner:
    """Fast local secret + PII scanner for CCR governance.

    Conservative by design: any finding switches CCR into redacted / non-storing
    mode rather than persisting raw credentials or user details. Previews never
    contain reconstructable secret material.
    """

    def scan(self, text: str) -> list[SecretFinding]:
        findings: list[SecretFinding] = []
        for kind, pattern, _pii in _PATTERNS:
            for m in pattern.finditer(text or ""):
                findings.append(SecretFinding(kind=kind, start=m.start(), end=m.end(), masked=_mask(kind, m.group(0))))
        return findings

    @staticmethod
    def has_pii(findings: list[SecretFinding]) -> bool:
        pii_kinds = {k for k, _p, pii in _PATTERNS if pii}
        return any(f.kind in pii_kinds for f in findings)

    def redact(self, text: str, findings: list[SecretFinding] | None = None) -> str:
        findings = findings if findings is not None else self.scan(text)
        if not findings:
            return text
        out: list[str] = []
        last = 0
        for f in sorted(findings, key=lambda x: x.start):
            if f.start < last:  # overlapping match — skip
                continue
            out.append(text[last:f.start])
            out.append(f"[REDACTED:{f.kind}]")
            last = f.end
        out.append(text[last:])
        return "".join(out)

    def summarize(self, text: str, findings: list[SecretFinding]) -> str:
        lines = (text or "").splitlines()
        kinds = sorted({f.kind for f in findings})
        return f"CCR payload withheld (sensitive content). chars={len(text or '')} lines={len(lines)} kinds={kinds}"
