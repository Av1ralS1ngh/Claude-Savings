from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, asdict
from functools import partial
from typing import Any

from .config import Settings
from .db import Store

# Try to import the YSCompress Rust-backed library.
# This is the real compression engine — SmartCrusher, CodeCompressor, Kompress-base.
# Falls back to a dumb local summarizer only if the library is not installed.
try:
    from headroom import compress as _yscompress_compress
    _YSCOMPRESS_AVAILABLE = True
except ImportError:
    _yscompress_compress = None
    _YSCOMPRESS_AVAILABLE = False


LOG_PATTERNS = [
    re.compile(r"Traceback \(most recent call last\):", re.I),
    re.compile(r"\b(ERROR|WARN|FATAL|Exception|stack trace)\b", re.I),
    re.compile(r"\{\s*\"", re.S),
]


@dataclass
class CompressionResult:
    messages: list[dict[str, Any]]
    tokens_before: int
    tokens_after: int
    tokens_saved: int
    compression_ratio: float
    mode: str
    engine: str
    transforms_applied: list[str]
    ccr_keys: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_tokens(value: Any) -> int:
    text = stringify(value)
    return max(1, int(len(text) / 4)) if text else 0


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def looks_compressible(text: str) -> bool:
    if len(text) > 4000:
        return True
    return any(p.search(text) for p in LOG_PATTERNS)


def _summarize_text_fallback(text: str, max_chars: int = 1800) -> str:
    """Last-resort local compressor — only used when yscompress library is not installed."""
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    important = []
    for line in lines:
        if re.search(r"ERROR|WARN|FATAL|Exception|Traceback|failed|denied|not found", line, re.I):
            important.append(line[:300])
        if len(important) >= 16:
            break
    head = text[: max_chars // 3]
    tail = text[-max_chars // 3 :]
    important_block = "\n".join(important)
    import hashlib
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return (
        f"[decompute:compressed_context sha256={digest} original_chars={len(text)}]\n"
        f"<head>\n{head}\n</head>\n"
        + (f"<important_lines>\n{important_block}\n</important_lines>\n" if important_block else "")
        + f"<tail>\n{tail}\n</tail>\n"
        f"[/decompute:compressed_context]"
    )


class YSCompressClient:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    async def compress_messages(
        self,
        messages: list[dict[str, Any]],
        mode: str | None = None,
        ccr: bool | None = None,
        preserve_latest_user: bool = True,
    ) -> CompressionResult:
        mode = mode or self.settings.yscompress_default_mode
        tokens_before = estimate_tokens(messages)

        if mode == "off":
            return CompressionResult(
                messages=messages,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                tokens_saved=0,
                compression_ratio=1.0,
                mode=mode,
                engine="off",
                transforms_applied=[],
                ccr_keys=[],
            )

        # ── Path 1: YSCompress Rust library (preferred) ────────────────────────
        if _YSCOMPRESS_AVAILABLE:
            try:
                loop = asyncio.get_running_loop()
                # compress() is sync + CPU-bound — run it in a thread to not block the event loop
                result = await loop.run_in_executor(
                    None,
                    partial(_yscompress_compress, messages, model="claude-sonnet-4-5")
                )
                tokens_after = result.tokens_after
                tokens_saved = max(0, tokens_before - tokens_after)
                return CompressionResult(
                    messages=result.messages,
                    tokens_before=tokens_before,
                    tokens_after=tokens_after,
                    tokens_saved=tokens_saved,
                    compression_ratio=round(tokens_after / max(tokens_before, 1), 4),
                    mode=mode,
                    engine="yscompress_rust",
                    transforms_applied=result.transforms_applied or ["yscompress"],
                    ccr_keys=[],
                )
            except Exception:
                pass  # fall through to local

        # ── Path 2: dumb local fallback (yscompress not installed) ─────────────
        compressed, transforms, ccr_keys = self._local_compress(
            messages, mode, preserve_latest_user
        )
        tokens_after = estimate_tokens(compressed)
        return CompressionResult(
            messages=compressed,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_saved=max(0, tokens_before - tokens_after),
            compression_ratio=round(tokens_after / max(tokens_before, 1), 4),
            mode=mode,
            engine="fallback_local",
            transforms_applied=transforms,
            ccr_keys=ccr_keys,
        )

    def _local_compress(
        self,
        messages: list[dict[str, Any]],
        mode: str,
        preserve_latest_user: bool,
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        out: list[dict[str, Any]] = []
        transforms: set[str] = set()
        ccr_keys: list[str] = []
        latest_user_idx = None
        if preserve_latest_user:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    latest_user_idx = i
                    break

        for idx, msg in enumerate(messages):
            new_msg = dict(msg)
            role = new_msg.get("role", "user")
            should_preserve = idx == latest_user_idx and role == "user"
            new_msg["content"] = self._compress_content(
                new_msg.get("content"),
                should_preserve=should_preserve,
                mode=mode,
                transforms=transforms,
                ccr_keys=ccr_keys,
                role=role,
            )
            out.append(new_msg)
        return out, sorted(transforms), ccr_keys

    def _compress_content(
        self,
        content: Any,
        *,
        should_preserve: bool,
        mode: str,
        transforms: set[str],
        ccr_keys: list[str],
        role: str,
    ) -> Any:
        max_chars = 1000 if mode == "aggressive" else 1800

        if isinstance(content, str):
            if should_preserve and not looks_compressible(content):
                return content
            if role in {"tool", "function"} or looks_compressible(content):
                transforms.add("fallback_smart_context")
                return _summarize_text_fallback(content, max_chars=max_chars)
            return content

        if isinstance(content, list):
            new_blocks = []
            for block in content:
                if not isinstance(block, dict):
                    new_blocks.append(block)
                    continue
                b = dict(block)
                btype = b.get("type")
                if btype == "tool_result":
                    raw = stringify(b.get("content"))
                    transforms.add("fallback_tool_result")
                    b["content"] = _summarize_text_fallback(raw, max_chars=max_chars)
                elif btype == "text" and isinstance(b.get("text"), str):
                    text = b["text"]
                    if not should_preserve and looks_compressible(text):
                        transforms.add("fallback_text_context")
                        b["text"] = _summarize_text_fallback(text, max_chars=max_chars)
                new_blocks.append(b)
            return new_blocks

        if isinstance(content, dict):
            raw = stringify(content)
            if looks_compressible(raw):
                transforms.add("fallback_json_context")
                return _summarize_text_fallback(raw, max_chars=max_chars)
        return content
