"""
CCR (Compress-Cache-Retrieve) reference-replacement stage.

Atomic, self-contained, and testable in isolation: given messages + a project, it
replaces bulky non-latest tool outputs with an encrypted-and-stored reference plus
a structural summary, and returns the new messages + the stored reference ids. The
gateway registers a ccr_retrieve tool and serves originals back on demand.

Security: originals are only elided when they can be stored ENCRYPTED. Content with
secrets or user PII is never stored (the store redacts it) and is therefore never
elided either, so nothing sensitive is persisted or hidden behind a lossy marker.
Summaries are secret-scrubbed as defense-in-depth.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .crypto import CCRCrypto
from .db import Store
from .secret_scanner import SecretScanner

# A bulky output above this token estimate is worth eliding behind a reference.
DEFAULT_MIN_TOKENS = 600

CCR_RETRIEVE_TOOL: dict[str, Any] = {
    "name": "ccr_retrieve",
    "description": (
        "Retrieve the full original content of a CCR reference that was elided from "
        "the context to save tokens. Pass the id from a [ccr:reference id=...] marker. "
        "Use this only when the structural summary in the marker is insufficient."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "the ccr_... reference id"}},
        "required": ["id"],
    },
}

_SIG_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:def |class |function |const \w+\s*=\s*\(|"
    r"let \w+\s*=\s*\(|pub fn |pub\(crate\) fn |fn |impl |interface |type \w+\s*=|"
    r"struct |enum |trait |func )",
    re.M,
)
_IMPORTANT_RE = re.compile(
    r"\b(ERROR|ERR|WARN|WARNING|FATAL|PANIC|Exception|Traceback|failed|denied|"
    r"timeout|refused|not found|OOM|exhausted|5\d\d|crash)\b",
    re.I,
)
_CODE_HINT_RE = re.compile(
    r"\b(def |class |import |function |const |let |pub fn |impl |interface )", re.M
)

# Verbose fields dropped during record pruning. These carry bulk but rarely the
# signal an agent reasons over; the full value stays retrievable via CCR.
_VERBOSE_FIELDS = frozenset({
    "avatar_url", "url", "html_url", "blob_url", "raw_url", "gravatar_id", "node_id",
    "sha", "body", "content", "description", "diff", "patch", "stack_trace", "traceback",
})
_MAX_FIELD_STR = 160  # truncate long string values to this many chars


# High-cardinality fields ignored when fingerprinting log records (they vary per line
# but don't change the event identity), so near-identical entries collapse together.
_VOLATILE_KEYS = frozenset({
    "timestamp", "ts", "time", "date", "datetime", "trace_id", "traceid", "span_id",
    "spanid", "request_id", "req_id", "requestid", "correlation_id", "id", "uuid", "guid",
    "host", "hostname", "pod", "node", "node_id", "thread", "tid", "pid", "seq",
    "sequence", "offset", "cursor", "elapsed_ms", "latency_ms", "duration_ms",
})
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEXID_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{8,}\b")


def _template_str(s: str) -> str:
    """Replace the variable tokens in a log string (uuids, hex ids, numbers) with '#'
    so two lines that differ only by those tokens fingerprint identically."""
    s = _UUID_RE.sub("#", s)
    s = _HEXID_RE.sub("#", s)
    return re.sub(r"\d+", "#", s)


def _record_fingerprint(rec: dict[str, Any]) -> str:
    """Stable signature of a record ignoring volatile fields + templating variable
    tokens. Distinct event types (esp. errors) keep distinct fingerprints."""
    def norm(v: Any) -> Any:
        if isinstance(v, str):
            return _template_str(v)[:160]
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, (int, float)):
            return 0
        if isinstance(v, list):
            return [norm(x) for x in v[:3]]
        if isinstance(v, dict):
            return {k: ("*" if k.lower() in _VOLATILE_KEYS else norm(x)) for k, x in sorted(v.items())}
        return v
    clean = {k: norm(v) for k, v in sorted(rec.items()) if k.lower() not in _VOLATILE_KEYS}
    return json.dumps(clean, sort_keys=True, ensure_ascii=False)[:480]


_LOG_LEVELS = frozenset({
    "trace", "debug", "info", "information", "notice", "warn", "warning", "error",
    "err", "fatal", "critical", "crit", "alert", "emergency",
})
_MSG_KEYS = ("message", "msg", "log", "event", "text")
_TS_KEYS = ("timestamp", "ts", "time", "@timestamp", "datetime", "date", "created_at")


def _looks_loglike(records: list[dict[str, Any]]) -> bool:
    """True only for arrays of LOG records — a recognized `level`, or a message+timestamp
    pair. Excludes distinct-record arrays (DB rows, code/file search hits, issues) so
    dedup never collapses records the agent must see individually."""
    hits = 0
    for r in records:
        lvl = r.get("level") or r.get("severity") or r.get("levelname") or r.get("loglevel")
        is_log = isinstance(lvl, str) and lvl.strip().lower() in _LOG_LEVELS
        if not is_log:
            is_log = any(k in r for k in _MSG_KEYS) and any(k in r for k in _TS_KEYS)
        if is_log:
            hits += 1
    return hits >= len(records) * 0.7


def _dedup_records(records: list[Any]) -> list[Any] | None:
    """Collapse a repetitive array of LOG records to one representative per event type +
    a `_repeats` count, KEEPING every distinct event (errors included). Only fires on
    genuine logs; distinct-record arrays (issues, code/file search, DB rows) keep every
    record."""
    if len(records) < 12 or not all(isinstance(r, dict) for r in records):
        return None
    if not _looks_loglike(records):
        return None
    fps = [_record_fingerprint(r) for r in records]
    if len(set(fps)) >= len(records) * 0.9:
        return None  # already near-distinct — nothing meaningful to collapse
    reps: dict[str, Any] = {}
    counts: dict[str, int] = {}
    order: list[str] = []
    for rec, fp in zip(records, fps):
        if fp not in reps:
            reps[fp] = rec
            counts[fp] = 0
            order.append(fp)
        counts[fp] += 1
    out: list[Any] = []
    for fp in order:
        rep = _prune_json(reps[fp])
        if isinstance(rep, dict) and counts[fp] > 1:
            rep = {**rep, "_repeats": counts[fp]}
        out.append(rep)
    return out


def _prune_json(value: Any, _depth: int = 0) -> Any:
    """Field-prune a JSON value while keeping every DISTINCT record. Drops verbose
    fields, truncates long strings, and collapses repetitive log-record arrays to one
    example per event type + a count — information-preserving (every distinct event
    survives, the agent can still reason over them), with full fidelity via CCR."""
    if isinstance(value, str):
        return value if len(value) <= _MAX_FIELD_STR else value[:_MAX_FIELD_STR] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        deduped = _dedup_records(value)
        if deduped is not None:
            return deduped
        return [_prune_json(x, _depth + 1) for x in value]  # keep ALL elements
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in _VERBOSE_FIELDS:
                continue
            out[k] = _prune_json(v, _depth + 1)
        return out
    return value


def _dedup_lines(text: str, max_groups: int = 200) -> str | None:
    """Collapse repeated log lines into one example per fingerprint + a count, keeping
    every distinct event (information-preserving). Returns None if not worth it."""
    lines = text.splitlines()
    if len(lines) < 20:
        return None
    groups: dict[str, list] = {}
    order: list[str] = []
    for ln in lines:
        fp = re.sub(r"\d+", "#", ln.strip())[:200]
        if fp not in groups:
            groups[fp] = [ln.strip()[:240], 0]
            order.append(fp)
        groups[fp][1] += 1
    if len(groups) >= len(lines) * 0.9:
        return None  # little repetition — dedup won't help
    out = []
    for fp in order[:max_groups]:
        example, count = groups[fp]
        out.append(f"x{count} | {example}" if count > 1 else example)
    return "\n".join(out)


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else (json.dumps(value, ensure_ascii=False) if value is not None else "")
    return max(1, len(text) // 4) if text else 0


def flatten_text(content: Any) -> str:
    """Flatten message/block content (string or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                if isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif "content" in b:
                    c = b["content"]
                    parts.append(c if isinstance(c, str) else flatten_text(c))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p)
    return json.dumps(content) if content is not None else ""


def _latest_user_index(messages: list[dict[str, Any]]) -> int | None:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i
    return None


class CCRStage:
    """Reference-replacement stage. One instance per request is cheap; all state
    lives in the injected store/crypto/scanner."""

    def __init__(self, store: Store, crypto: CCRCrypto, scanner: SecretScanner, *, min_tokens: int = DEFAULT_MIN_TOKENS):
        self._store = store
        self._crypto = crypto
        self._scanner = scanner
        self._min_tokens = min_tokens

    @property
    def active(self) -> bool:
        return bool(self._crypto.enabled)

    def apply(self, messages: list[dict[str, Any]], *, project_id: str, preserve_latest_user: bool = True) -> tuple[list[dict[str, Any]], list[str]]:
        """Return (new_messages, ccr_ids). The latest user instruction is never elided."""
        latest = _latest_user_index(messages) if preserve_latest_user else None
        out: list[dict[str, Any]] = []
        keys: list[str] = []
        for idx, msg in enumerate(messages):
            if idx == latest:
                out.append(msg)
                continue
            new_msg = dict(msg)
            new_content, k = self._content(new_msg.get("content"), project_id)
            new_msg["content"] = new_content
            keys.extend(k)
            out.append(new_msg)
        return out, keys

    def _content(self, content: Any, project_id: str) -> tuple[Any, list[str]]:
        if isinstance(content, str):
            if estimate_tokens(content) >= self._min_tokens:
                marker, key = self._store_and_mark(content, project_id)
                if key:
                    return marker, [key]
            return content, []
        if isinstance(content, list):
            new_blocks: list[Any] = []
            keys: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = flatten_text(block.get("content"))
                    if estimate_tokens(text) >= self._min_tokens:
                        marker, key = self._store_and_mark(text, project_id)
                        if key:
                            b = dict(block)
                            b["content"] = marker
                            new_blocks.append(b)
                            keys.append(key)
                            continue
                new_blocks.append(block)
            return new_blocks, keys
        return content, []

    # Only elide a tool output if we can compress it WITHOUT dropping records —
    # i.e. information-preserving compression that the agent can still reason over.
    # If we can't (the result wouldn't be meaningfully smaller, or it isn't a shape
    # we can prune safely), we leave the content in place for Headroom's compressor.
    _GAIN_THRESHOLD = 0.85  # must shrink to <85% of original to be worth eliding

    def _store_and_mark(self, text: str, project_id: str) -> tuple[str, str | None]:
        compressed = self._compress_preserving(text)
        if compressed is None:
            return text, None  # nothing information-preserving to do — leave for Headroom
        item = self._store.put_ccr(project_id=project_id, original_text=text, metadata={"ccr": "compressed"}, crypto=self._crypto, scanner=self._scanner)
        # Secret/PII content is stored 'redacted' (no payload) — don't elide it.
        if item.get("storage_mode") != "encrypted":
            return text, None
        compressed = self._scanner.redact(compressed)  # defense-in-depth
        marker = (
            f"[ccr:compressed id={item['id']} original_tokens={estimate_tokens(text)} "
            f"note=verbose-fields-trimmed-all-records-kept]\n"
            f"{compressed}\n"
            f"[Full untrimmed content retrievable via the ccr_retrieve tool, id={item['id']}.]"
        )
        if estimate_tokens(marker) < estimate_tokens(text):
            return marker, item["id"]
        return text, None

    @classmethod
    def _compress_preserving(cls, text: str) -> str | None:
        """Return an information-preserving compressed form (ALL records kept), or
        None if no safe, worthwhile compression applies."""
        before = estimate_tokens(text)
        # 1) Structured JSON → field-prune EVERY record (drop verbose fields only).
        stripped = text.lstrip()
        if stripped[:1] in "[{":
            try:
                pruned = json.dumps(_prune_json(json.loads(text)), ensure_ascii=False)
                if estimate_tokens(pruned) <= before * cls._GAIN_THRESHOLD:
                    return pruned
            except Exception:
                pass
        # 2) Source code → public signatures (bodies dropped; reversible via CCR).
        if _CODE_HINT_RE.search(text):
            sigs = [m.group(0).strip()[:160] for m in _SIG_RE.finditer(text)][:60]
            block = "\n".join(sigs)
            if sigs and estimate_tokens(block) <= before * cls._GAIN_THRESHOLD:
                return f"<source signatures={len(sigs)}>\n{block}\n</source>"
        # 3) Logs / repetitive text → dedup (keep one example per event + count).
        dedup = _dedup_lines(text)
        if dedup is not None and estimate_tokens(dedup) <= before * cls._GAIN_THRESHOLD:
            return dedup
        return None
