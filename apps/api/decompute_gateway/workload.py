"""
Workload classification.

Hina asked for "workload-class metrics" so savings can be reported by workload
rather than as a single misleading global average. Each request is classified by
the dominant content type of its bulk (the largest tool_result / message content),
which is also *why* it compressed as much (or as little) as it did:

  structured_json : JSON tool output / API responses   → SmartCrusher (high savings)
  logs            : repetitive log / stack-trace output → log dedup (high savings)
  source_code     : source files / code                 → needs AST (low without it)
  prose           : natural-language text               → Kompress (needs PyTorch)
  short           : below the compression threshold      → passthrough
  mixed           : no single dominant type

The classification is intentionally cheap and deterministic (regex/structure
sniffing), and is computed once per request at record time.
"""
from __future__ import annotations

import json
import re
from typing import Any

_CODE_RE = re.compile(
    r"^\s*(def |class |async def |import |from \S+ import |function |const |let |var |"
    r"pub fn |impl |interface |type \w+ ?=|export (default |function |class ))",
    re.M,
)
_CODE_FENCE_RE = re.compile(r"```(?:python|py|ts|tsx|js|jsx|rust|rs|go|java|cpp|c|sql|sh|bash)", re.I)
_LOG_RE = re.compile(r"\b(ERROR|WARN|WARNING|FATAL|INFO|DEBUG|TRACE|Traceback|Exception|panicked|FAILED|stack trace)\b")
_LOG_LINE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}[T ]|\[\d|\d+:\d+:\d+|(INFO|ERROR|WARN|DEBUG)\b)", re.M)
_JSON_HINT_RE = re.compile(r'^\s*[\[{]')

SHORT_TOKEN_THRESHOLD = 400  # ~1500 chars; below this nothing meaningfully compresses

CLASSES = ("structured_json", "logs", "source_code", "prose", "short", "mixed")


def _text_of(content: Any) -> str:
    """Flatten a message content (string or list of blocks) to text.

    Anthropic tool_result content is frequently a LIST of inner blocks, e.g.
    {"type":"tool_result","content":[{"type":"text","text": "...logs..."}]}.
    We must recurse into that inner text — NOT json.dumps it — otherwise the
    wrapped text looks like JSON and every log/code/prose payload misclassifies
    as structured_json."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif "content" in block:
                    c = block["content"]
                    parts.append(c if isinstance(c, str) else _text_of(c))
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(_text_of(block))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if "content" in content:
            c = content["content"]
            return c if isinstance(c, str) else _text_of(c)
        return json.dumps(content)
    return json.dumps(content) if content is not None else ""


def _is_json(text: str) -> bool:
    head = text.lstrip()[:1]
    if head not in "[{":
        return False
    try:
        json.loads(text)
        return True
    except Exception:
        # Large valid-looking JSON that's truncated still counts structurally.
        return bool(_JSON_HINT_RE.match(text)) and text.count('"') > 20


def _classify_block(text: str) -> str:
    if _is_json(text):
        return "structured_json"
    log_lines = len(_LOG_LINE_RE.findall(text))
    if log_lines >= 5 or (_LOG_RE.search(text) and text.count("\n") > 20):
        return "logs"
    if _CODE_FENCE_RE.search(text) or len(_CODE_RE.findall(text)) >= 3:
        return "source_code"
    return "prose"


def classify_workload(messages: list[dict[str, Any]]) -> str:
    """Classify a request by the dominant content type of its largest block."""
    if not messages:
        return "short"

    # Gather (size, class) for every message's content.
    sized: list[tuple[int, str]] = []
    total_chars = 0
    for m in messages:
        text = _text_of(m.get("content"))
        total_chars += len(text)
        if text:
            sized.append((len(text), _classify_block(text)))

    if total_chars < SHORT_TOKEN_THRESHOLD * 4:
        return "short"
    if not sized:
        return "short"

    # Aggregate char weight per class; the dominant class wins if it owns the bulk.
    weight: dict[str, int] = {}
    for size, cls in sized:
        weight[cls] = weight.get(cls, 0) + size
    dominant = max(weight, key=weight.get)
    if weight[dominant] >= 0.6 * total_chars:
        return dominant
    return "mixed"
