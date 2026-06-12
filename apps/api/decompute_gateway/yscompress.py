from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from functools import partial
import hashlib
import json
import re
from typing import Any

import httpx

from .config import Settings
from .crypto import CCRCrypto
from .db import Store
from .secret_scanner import SecretScanner

# Try to import the Rust-backed headroom library.
# The internal Python module is named `headroom` (intentional — see CLAUDE.md).
# This is the SmartCrusher / CodeCompressor / Kompress-base pipeline built by
# the Headroom engineers. It must be the primary compression path.
# Falls back to the dumb local summariser only if the library is not installed.
try:
    from headroom import compress as _headroom_compress
    _HEADROOM_AVAILABLE = True
except ImportError:
    _headroom_compress = None
    _HEADROOM_AVAILABLE = False


LOG_PATTERNS = [
    re.compile(r"Traceback \(most recent call last\):", re.I),
    re.compile(r"\b(ERROR|WARN|FATAL|Exception|stack trace|failed|denied|not found)\b", re.I),
    re.compile(r"\{\s*\"", re.S),
]
CODE_PATTERNS = [
    re.compile(r"\b(def|class|import|from|function|const|let|var|interface|type|struct|enum|impl)\b"),
    re.compile(r"^[\t ]*(if|for|while|try|except|catch|return)\b", re.M),
    re.compile(r"```[a-zA-Z0-9_-]*\n"),
]
HTML_PATTERNS = [
    re.compile(r"<!doctype html", re.I),
    re.compile(r"<html\b", re.I),
    re.compile(r"<head\b", re.I),
    re.compile(r"<body\b", re.I),
    re.compile(r"<script\b", re.I),
    re.compile(r"<style\b", re.I),
    re.compile(r"<div\b", re.I),
]

# CCR reference-replacement is an atomic, self-contained stage (see ccr.py).
# Re-exported here so existing imports (`from .yscompress import CCR_RETRIEVE_TOOL`)
# keep working.
from .ccr import CCR_RETRIEVE_TOOL, CCRStage, scrub_messages  # noqa: E402


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


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def estimate_tokens(value: Any) -> int:
    text = stringify(value)
    return max(1, int(len(text) / 4)) if text else 0


def looks_source_code(text: str) -> bool:
    if not text or len(text) < 40:
        return False
    hits = sum(1 for p in CODE_PATTERNS if p.search(text))
    return hits >= 2 or (";" in text and "{" in text and "}" in text)


def looks_html_content(text: str) -> bool:
    if not text or len(text) < 80:
        return False
    head = text[:8000]
    return sum(1 for p in HTML_PATTERNS if p.search(head)) >= 2


def looks_compressible(text: str) -> bool:
    if len(text) > 4000 and not looks_source_code(text):
        return True
    return any(p.search(text) for p in LOG_PATTERNS)


def summarize_text(text: str, max_chars: int = 1800) -> str:
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    important: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if re.search(r"ERROR|WARN|FATAL|Exception|Traceback|failed|denied|not found|AssertionError", line, re.I):
            normalized = re.sub(r"\d+", "#", line[:300])
            if normalized not in seen:
                seen.add(normalized)
                important.append(line[:300])
        if len(important) >= 20:
            break
    head = text[: max_chars // 3]
    tail = text[-max_chars // 3 :]
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return (
        f"[yscompress:compressed_context sha256={digest} original_chars={len(text)} original_lines={len(lines)}]\n"
        f"<head>\n{head}\n</head>\n"
        + (f"<important_lines>\n" + "\n".join(important) + "\n</important_lines>\n" if important else "")
        + f"<tail>\n{tail}\n</tail>\n"
        f"[/yscompress:compressed_context]"
    )


def _large_cache_control_text(block: dict[str, Any], *, min_chars: int = 4000) -> bool:
    if "cache_control" not in block:
        return False
    btype = block.get("type")
    if btype == "text" and isinstance(block.get("text"), str):
        return len(block["text"]) >= min_chars
    if btype == "tool_result":
        raw = block.get("content")
        if isinstance(raw, str):
            return len(raw) >= min_chars
        return len(stringify(raw)) >= min_chars
    return False


def release_large_cache_control_blocks(messages: list[dict[str, Any]], *, min_chars: int = 4000) -> tuple[list[dict[str, Any]], int]:
    """Remove per-block cache_control only from large attached text/tool outputs.

    Claude Code marks attached files/tool outputs with cache_control so repeated
    turns can hit the provider prefix cache. The gateway already has a route-level
    cost guard: for huge cache-controlled contexts it disables compression entirely
    to preserve that cache. Below that threshold, compression should win, but
    Headroom's per-block cache guard would still refuse to touch these blocks and
    HTML/log attachments would show ~0% savings. This releases the cache marker
    only for large blocks that we intentionally send through compression; if the
    compression validator rejects the result, the original messages (with
    cache_control intact) are returned.
    """
    released = 0
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        changed = False
        blocks: list[Any] = []
        for block in content:
            if isinstance(block, dict) and _large_cache_control_text(block, min_chars=min_chars):
                b = dict(block)
                b.pop("cache_control", None)
                blocks.append(b)
                changed = True
                released += 1
            else:
                blocks.append(block)
        out.append({**msg, "content": blocks} if changed else msg)
    return out, released


class YSCompressClient:
    def __init__(self, settings: Settings, store: Store, crypto: CCRCrypto, scanner: SecretScanner):
        self.settings = settings
        self.store = store
        self.crypto = crypto
        self.scanner = scanner
        self._ccr = CCRStage(store, crypto, scanner)

    async def compress_system(
        self,
        system: Any,
        *,
        mode: str | None = None,
        min_chars: int = 4000,
    ) -> tuple[Any, int, int, list[str]]:
        """Compress large HTML attachments that Claude Code places in system blocks.

        Claude Code Read-tool output lands in messages, but attached/context files
        can be sent as top-level Anthropic `system` content blocks. The gateway
        historically compressed only `messages`, so these HTML files recorded
        almost no savings even though the same bytes compress well through
        ContentRouter. To avoid mutating Claude Code's core instructions, this
        method targets only large HTML-looking text blocks and leaves all other
        system content untouched.
        """
        mode = mode or self.settings.yscompress_default_mode
        before = estimate_tokens(system)
        if mode == "off" or not system:
            return system, before, before, []

        if isinstance(system, str):
            if len(system) < min_chars or not looks_html_content(system):
                return system, before, before, []
            compressed, transforms = await self._compress_single_large_text(system)
            after_text = estimate_tokens(compressed)
            if after_text < before * 0.99:
                return compressed, before, after_text, [f"system:{t}" for t in transforms]
            return system, before, before, []

        if not isinstance(system, list):
            return system, before, before, []

        changed = False
        out: list[Any] = []
        transforms: list[str] = []
        for block in system:
            if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
                out.append(block)
                continue
            text = block["text"]
            if len(text) < min_chars or not looks_html_content(text):
                out.append(block)
                continue
            compressed, block_transforms = await self._compress_single_large_text(text)
            if estimate_tokens(compressed) >= estimate_tokens(text) * 0.99:
                out.append(block)
                continue
            b = dict(block)
            b["text"] = compressed
            b.pop("cache_control", None)
            out.append(b)
            changed = True
            transforms.extend(f"system:{t}" for t in block_transforms)

        if not changed:
            return system, before, before, []
        after = estimate_tokens(out)
        return out, before, after, transforms or ["system:html"]

    async def _compress_single_large_text(self, text: str) -> tuple[str, list[str]]:
        if _HEADROOM_AVAILABLE:
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    partial(
                        _headroom_compress,
                        [{"role": "user", "content": [{"type": "text", "text": text}]}],
                        model="claude-haiku-4-5",
                        compress_user_messages=True,
                        compress_system_messages=True,
                    ),
                )
                content = result.messages[0].get("content") if result.messages else None
                if isinstance(content, list) and content and isinstance(content[0], dict):
                    compressed = content[0].get("text")
                    if isinstance(compressed, str) and compressed:
                        return compressed, list(result.transforms_applied or ["html"])
            except Exception:
                pass
        return summarize_text(text), ["fallback_html_summary"]

    async def compress_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        project_id: str,
        mode: str | None = None,
        ccr: bool | None = None,
        preserve_latest_user: bool = True,
        compress_cache_control_large_blocks: bool = False,
    ) -> CompressionResult:
        mode = mode or self.settings.yscompress_default_mode
        ccr_enabled = self.settings.ccr_enabled if ccr is None else bool(ccr)
        tokens_before = estimate_tokens(messages)
        # Privacy filter (always on, even when compression is off): hard-redact secrets
        # and tokenize/redact PII BEFORE anything reaches Claude or the store.
        messages = scrub_messages(messages, self.scanner, preserve_latest_user=preserve_latest_user)
        if mode == "off":
            after = estimate_tokens(messages)
            return CompressionResult(messages=messages, tokens_before=tokens_before, tokens_after=after, tokens_saved=max(0, tokens_before - after), compression_ratio=round(after / max(tokens_before, 1), 4), mode=mode, engine="off", transforms_applied=["privacy_scrub"], ccr_keys=[])

        # ── Stage 0: iterative CCR (reference-replacement) ─────────────────────
        # Replace bulky tool outputs with an encrypted-and-stored reference + a
        # structural summary, BEFORE structural compression runs. CCR is only
        # active when a real encryption key is configured (otherwise originals
        # can't be stored safely, so we must not elide them).
        stage_messages = messages
        ccr_keys: list[str] = []
        ccr_transforms: list[str] = []
        if ccr_enabled and self._ccr.active:
            stage_messages, ccr_keys = self._ccr.apply(messages, project_id=project_id, preserve_latest_user=preserve_latest_user)
            if ccr_keys:
                ccr_transforms = [f"ccr:reference_replacement:{len(ccr_keys)}"]

        cache_control_transforms: list[str] = []
        if compress_cache_control_large_blocks:
            stage_messages, released = release_large_cache_control_blocks(stage_messages)
            if released:
                cache_control_transforms = [f"cache_control:large_block_released:{released}"]

        # ── Stage 1: Rust core via headroom Python library (preferred) ─────────
        # compress() is sync + CPU-bound — run in executor to avoid blocking the
        # event loop. SmartCrusher / CodeCompressor / Kompress-base.
        if _HEADROOM_AVAILABLE:
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    partial(_headroom_compress, stage_messages, model="claude-haiku-4-5", compress_user_messages=self.settings.compress_user_messages)
                )
                final_messages = result.messages
                tokens_after = estimate_tokens(final_messages)
                engine = "yscompress_rust" + ("+ccr" if ccr_keys else "")
                transforms = list(result.transforms_applied or ["yscompress"]) + ccr_transforms + cache_control_transforms
                return self._validated_result(
                    original=messages,
                    compressed=final_messages,
                    tokens_before=tokens_before,
                    tokens_after=tokens_after,
                    mode=mode,
                    engine=engine,
                    transforms=transforms,
                    ccr_keys=ccr_keys,
                    min_savings_ratio=0.01 if cache_control_transforms else 0.0,
                )
            except Exception:
                pass  # fall through to local fallback

        compressed, transforms, fb_ccr_keys = self._fallback_compress(stage_messages, mode=mode, ccr_enabled=ccr_enabled, project_id=project_id, preserve_latest_user=preserve_latest_user)
        tokens_after = estimate_tokens(compressed)
        return self._validated_result(
            original=messages,
            compressed=compressed,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            mode=mode,
            engine="yscompress_fallback_local" + ("+ccr" if ccr_keys else ""),
            transforms=sorted(transforms) + ccr_transforms + cache_control_transforms,
            ccr_keys=ccr_keys + fb_ccr_keys,
            min_savings_ratio=0.01 if cache_control_transforms else 0.0,
        )

    @staticmethod
    def _validated_result(
        *,
        original: list[dict[str, Any]],
        compressed: list[dict[str, Any]],
        tokens_before: int,
        tokens_after: int,
        mode: str,
        engine: str,
        transforms: list[str],
        ccr_keys: list[str],
        min_savings_ratio: float = 0.0,
    ) -> CompressionResult:
        """Token-validate the compression. If it did not actually shrink the
        context, discard it and pass the ORIGINAL messages through unchanged.
        Compression must never make the payload bigger — that would cost the
        customer more tokens and bust the cache."""
        tokens_saved = tokens_before - tokens_after
        if tokens_after >= tokens_before or (
            min_savings_ratio > 0 and tokens_before > 0 and tokens_saved / tokens_before < min_savings_ratio
        ):
            return CompressionResult(
                messages=original,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                tokens_saved=0,
                compression_ratio=1.0,
                mode=mode,
                engine=f"{engine}:passthrough_no_gain",
                transforms_applied=[],
                ccr_keys=[],
            )
        return CompressionResult(
            messages=compressed,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_saved=tokens_before - tokens_after,
            compression_ratio=round(tokens_after / max(tokens_before, 1), 4),
            mode=mode,
            engine=engine,
            transforms_applied=transforms,
            ccr_keys=ccr_keys,
        )

    def _put_ccr(self, project_id: str, text: str, metadata: dict[str, Any], enabled: bool) -> str | None:
        if not enabled or len(text) < 1500:
            return None
        item = self.store.put_ccr(project_id=project_id, original_text=text, metadata=metadata, crypto=self.crypto, scanner=self.scanner)
        return item["id"]

    def _fallback_compress(self, messages: list[dict[str, Any]], *, mode: str, ccr_enabled: bool, project_id: str, preserve_latest_user: bool) -> tuple[list[dict[str, Any]], set[str], list[str]]:
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
            role = str(new_msg.get("role", "user"))
            should_preserve = idx == latest_user_idx and role == "user"
            new_msg["content"] = self._compress_content(
                new_msg.get("content"),
                project_id=project_id,
                role=role,
                should_preserve=should_preserve,
                mode=mode,
                ccr_enabled=ccr_enabled,
                transforms=transforms,
                ccr_keys=ccr_keys,
            )
            out.append(new_msg)
        return out, transforms, ccr_keys

    def _compress_content(self, content: Any, *, project_id: str, role: str, should_preserve: bool, mode: str, ccr_enabled: bool, transforms: set[str], ccr_keys: list[str]) -> Any:
        max_chars = 1000 if mode == "aggressive" else 1800
        if isinstance(content, str):
            if should_preserve and not looks_compressible(content):
                return content
            if looks_source_code(content) and role == "user":
                return content
            if role in {"tool", "function"} or looks_compressible(content):
                ccr_id = self._put_ccr(project_id, content, {"role": role, "mode": mode}, ccr_enabled)
                if ccr_id:
                    ccr_keys.append(ccr_id)
                transforms.add("fallback_log_tool_context")
                return summarize_text(content, max_chars=max_chars)
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
                    ccr_id = self._put_ccr(project_id, raw, {"block_type": btype, "mode": mode}, ccr_enabled)
                    if ccr_id:
                        ccr_keys.append(ccr_id)
                    transforms.add("fallback_tool_result")
                    b["content"] = summarize_text(raw, max_chars=max_chars)
                elif btype == "text" and isinstance(b.get("text"), str):
                    text = b["text"]
                    if not should_preserve and looks_compressible(text) and not looks_source_code(text):
                        ccr_id = self._put_ccr(project_id, text, {"block_type": btype, "mode": mode}, ccr_enabled)
                        if ccr_id:
                            ccr_keys.append(ccr_id)
                        transforms.add("fallback_text_context")
                        b["text"] = summarize_text(text, max_chars=max_chars)
                new_blocks.append(b)
            return new_blocks
        if isinstance(content, dict):
            raw = stringify(content)
            if looks_compressible(raw):
                ccr_id = self._put_ccr(project_id, raw, {"role": role, "mode": mode}, ccr_enabled)
                if ccr_id:
                    ccr_keys.append(ccr_id)
                transforms.add("fallback_json_context")
                return summarize_text(raw, max_chars=max_chars)
        return content
