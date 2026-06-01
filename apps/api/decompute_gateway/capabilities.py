"""
Runtime capability detection.

The dashboard must report the TRUTH about what the compressor can actually do —
not just which feature flags are set. Earlier the dashboard showed "CCR: enabled"
while CCR did nothing (the rust path ignores the flag and no encryption key was
configured). This module distinguishes a flag being *set* from a capability being
*active*, and explains the gap.

Detected capabilities:
  - rust_core      : the headroom Rust compressor (SmartCrusher/CodeCompressor/...)
  - kompress_ml    : PyTorch-backed Kompress-base ML text compression (§8.6)
  - ast_code_aware : AST/tree-sitter source-code compression (headroom-ai[code])
  - ccr_active     : CCR reference-replacement actually wired AND keyed AND enabled
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class Capability:
    name: str
    active: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@lru_cache(maxsize=1)
def _detect_rust_core() -> Capability:
    try:
        import headroom  # noqa: F401
        from headroom import compress  # noqa: F401
        ver = getattr(headroom, "__version__", "?")
        return Capability("rust_core", True, f"headroom {ver} importable; SmartCrusher/CodeCompressor active")
    except Exception as exc:  # pragma: no cover - only when lib absent
        return Capability("rust_core", False, f"headroom not importable ({type(exc).__name__}); using local fallback")


@lru_cache(maxsize=1)
def _detect_kompress_ml() -> Capability:
    try:
        import torch  # noqa: F401
        return Capability("kompress_ml", True, f"torch {torch.__version__}; Kompress-base ML text compression available")
    except Exception:
        return Capability("kompress_ml", False, "PyTorch not installed; ML text compression degraded (tokenizer-only)")


@lru_cache(maxsize=1)
def _detect_ast_code_aware() -> Capability:
    # Code-aware compression ships as an extra (headroom-ai[code] / tree-sitter).
    for probe in ("headroom.code", "headroom.compression.code", "tree_sitter"):
        try:
            __import__(probe)
            return Capability("ast_code_aware", True, f"{probe} present; source-code AST compression available")
        except Exception:
            continue
    return Capability("ast_code_aware", False, "AST/code-aware support not installed; source-code contexts compress structurally only")


def _detect_ccr_active(settings: Any) -> Capability:
    """CCR is only *active* if all three hold:
      1. the flag is enabled,
      2. an encryption key is configured (else originals can't be stored safely),
      3. it is wired into the active compression path.
    (3) is now True: compress_messages runs a CCR reference-replacement stage that
    stores bulky tool outputs encrypted and elides them behind a marker + retrieve
    tool. So CCR is active iff the flag is on AND a key is configured.
    """
    enabled = bool(getattr(settings, "ccr_enabled", False))
    keyed = bool(getattr(settings, "ccr_encryption_key", None))
    if enabled and keyed:
        return Capability("ccr_active", True, "CCR reference-replacement active (encrypted store + ccr_retrieve tool)")
    missing = []
    if not enabled:
        missing.append("flag off")
    if not keyed:
        missing.append("no encryption key")
    return Capability("ccr_active", False, "CCR inactive (" + ", ".join(missing) + ")")


def detect_capabilities(settings: Any) -> dict[str, Capability]:
    return {
        c.name: c
        for c in (
            _detect_rust_core(),
            _detect_kompress_ml(),
            _detect_ast_code_aware(),
            _detect_ccr_active(settings),
        )
    }


def capabilities_dict(settings: Any) -> dict[str, dict[str, Any]]:
    return {name: cap.to_dict() for name, cap in detect_capabilities(settings).items()}
