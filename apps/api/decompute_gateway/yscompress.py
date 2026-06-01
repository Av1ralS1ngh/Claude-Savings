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

# CCR reference-replacement is an atomic, self-contained stage (see ccr.py).
# Re-exported here so existing imports (`from .yscompress import CCR_RETRIEVE_TOOL`)
# keep working.
from .ccr import CCR_RETRIEVE_TOOL, CCRStage  # noqa: E402


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


class YSCompressClient:
    def __init__(self, settings: Settings, store: Store, crypto: CCRCrypto, scanner: SecretScanner):
        self.settings = settings
        self.store = store
        self.crypto = crypto
        self.scanner = scanner
        self._ccr = CCRStage(store, crypto, scanner)

    async def compress_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        project_id: str,
        mode: str | None = None,
        ccr: bool | None = None,
        preserve_latest_user: bool = True,
    ) -> CompressionResult:
        mode = mode or self.settings.yscompress_default_mode
        ccr_enabled = self.settings.ccr_enabled if ccr is None else bool(ccr)
        tokens_before = estimate_tokens(messages)
        if mode == "off":
            return CompressionResult(messages=messages, tokens_before=tokens_before, tokens_after=tokens_before, tokens_saved=0, compression_ratio=1.0, mode=mode, engine="off", transforms_applied=[], ccr_keys=[])

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

        # ── Stage 1: Rust core via headroom Python library (preferred) ─────────
        # compress() is sync + CPU-bound — run in executor to avoid blocking the
        # event loop. SmartCrusher / CodeCompressor / Kompress-base.
        if _HEADROOM_AVAILABLE:
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    partial(_headroom_compress, stage_messages, model="claude-haiku-4-5")
                )
                final_messages = result.messages
                tokens_after = estimate_tokens(final_messages)
                engine = "yscompress_rust" + ("+ccr" if ccr_keys else "")
                transforms = list(result.transforms_applied or ["yscompress"]) + ccr_transforms
                return self._validated_result(
                    original=messages,
                    compressed=final_messages,
                    tokens_before=tokens_before,
                    tokens_after=tokens_after,
                    mode=mode,
                    engine=engine,
                    transforms=transforms,
                    ccr_keys=ccr_keys,
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
            transforms=sorted(transforms) + ccr_transforms,
            ccr_keys=ccr_keys + fb_ccr_keys,
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
    ) -> CompressionResult:
        """Token-validate the compression. If it did not actually shrink the
        context, discard it and pass the ORIGINAL messages through unchanged.
        Compression must never make the payload bigger — that would cost the
        customer more tokens and bust the cache."""
        if tokens_after >= tokens_before:
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
