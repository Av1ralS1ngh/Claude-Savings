from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict
from typing import Any

import httpx

from .config import Settings
from .db import Store


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
    # Conservative-ish approximate tokenizer: 1 token ~= 4 chars for English/code mixed text.
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


def summarize_text(text: str, max_chars: int = 1800) -> str:
    """Safe fallback compressor for local development.

    It keeps the head, tail, and detected error lines. Headroom should replace this when
    YSCOMPRESS_BASE_URL is configured.
    """
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
        ccr_enabled = self.settings.yscompress_ccr_enabled if ccr is None else ccr
        tokens_before = estimate_tokens(messages)

        # Prefer real Headroom if configured. The fallback keeps development self-contained.
        if self.settings.yscompress_base_url:
            try:
                async with httpx.AsyncClient(timeout=self.settings.yscompress_timeout_s) as client:
                    resp = await client.post(
                        self.settings.yscompress_base_url.rstrip("/") + "/v1/compress",
                        json={"messages": messages, "mode": mode, "ccr": ccr_enabled},
                    )
                if resp.status_code < 400:
                    payload = resp.json()
                    compressed = payload.get("messages") or payload.get("compressed_messages") or messages
                    tokens_after = payload.get("tokens_after") or estimate_tokens(compressed)
                    tokens_before_remote = payload.get("tokens_before") or tokens_before
                    saved = max(0, int(tokens_before_remote) - int(tokens_after))
                    return CompressionResult(
                        messages=compressed,
                        tokens_before=int(tokens_before_remote),
                        tokens_after=int(tokens_after),
                        tokens_saved=saved,
                        compression_ratio=round(int(tokens_after) / max(int(tokens_before_remote), 1), 4),
                        mode=mode,
                        engine="yscompress_proxy",
                        transforms_applied=payload.get("transforms_applied") or payload.get("transforms") or ["yscompress"],
                        ccr_keys=payload.get("ccr_hashes") or payload.get("ccr_keys") or [],
                    )
            except Exception:
                # Fall through to local compressor.
                pass

        compressed, transforms, ccr_keys = self._fallback_compress(messages, mode, ccr_enabled, preserve_latest_user)
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

    def _fallback_compress(
        self,
        messages: list[dict[str, Any]],
        mode: str,
        ccr_enabled: bool,
        preserve_latest_user: bool,
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        if mode == "off":
            return messages, [], []

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
            # Preserve the latest plain user instruction. Compress tool outputs and older huge context.
            should_preserve = idx == latest_user_idx and role == "user"
            new_msg["content"] = self._compress_content(
                new_msg.get("content"),
                should_preserve=should_preserve,
                mode=mode,
                ccr_enabled=ccr_enabled,
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
        ccr_enabled: bool,
        transforms: set[str],
        ccr_keys: list[str],
        role: str,
    ) -> Any:
        if isinstance(content, str):
            if should_preserve and not looks_compressible(content):
                return content
            if role in {"tool", "function"} or looks_compressible(content):
                if ccr_enabled and len(content) > 1500:
                    ccr_keys.append(self.store.put_ccr(content, {"role": role, "mode": mode}))
                transforms.add("fallback_smart_context")
                return summarize_text(content, max_chars=1000 if mode == "aggressive" else 1800)
            return content

        # Anthropic content blocks: [{type:text/tool_result, ...}]
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
                    if ccr_enabled and len(raw) > 1500:
                        ccr_keys.append(self.store.put_ccr(raw, {"block_type": btype, "mode": mode}))
                    transforms.add("fallback_tool_result")
                    b["content"] = summarize_text(raw, max_chars=1000 if mode == "aggressive" else 1800)
                elif btype == "text" and isinstance(b.get("text"), str):
                    text = b["text"]
                    if not should_preserve and looks_compressible(text):
                        if ccr_enabled and len(text) > 1500:
                            ccr_keys.append(self.store.put_ccr(text, {"block_type": btype, "mode": mode}))
                        transforms.add("fallback_text_context")
                        b["text"] = summarize_text(text, max_chars=1000 if mode == "aggressive" else 1800)
                new_blocks.append(b)
            return new_blocks

        if isinstance(content, dict):
            raw = stringify(content)
            if looks_compressible(raw):
                if ccr_enabled:
                    ccr_keys.append(self.store.put_ccr(raw, {"role": role, "mode": mode}))
                transforms.add("fallback_json_context")
                return summarize_text(raw, max_chars=1000 if mode == "aggressive" else 1800)
        return content
