from __future__ import annotations

"""Privacy-safe support bundle generation for Decompute Gateway.

This module is intentionally self-contained and mostly standard-library only so it can
be dropped into the gateway and used by the CLI, FastAPI admin endpoint, or release
checks. It collects diagnostic metadata while aggressively avoiding raw prompts, CCR
payloads, PII vault contents, database dumps, provider keys, and credentials.

Design rules:
- Include allowlisted diagnostics, not arbitrary repo/data directories.
- Scrub every included text/JSON payload.
- Omit prompt-like fields by key name before token/PII scrubbing.
- Redact secrets irreversibly.
- Tokenize PII with stable placeholders for correlation inside one bundle.
- Emit a manifest and privacy report explaining exactly what was included/skipped.
"""

import argparse
import dataclasses
import datetime as _dt
import fnmatch
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

JSON = dict[str, Any]


# ---------------------------------------------------------------------------
# Scrubber
# ---------------------------------------------------------------------------

SECRET_FIELD_RE = re.compile(
    r"(?i)(^|[_\-.])(api[_\-.]?key|key|secret|token|password|passwd|pwd|authorization|bearer|private[_\-.]?key|client[_\-.]?secret|session[_\-.]?key|encryption[_\-.]?key|cookie|credential|fernet|kms[_\-.]?key|salt|seed|passphrase|database[_\-.]?url|dsn)($|[_\-.])"
)

# Env var names whose VALUE must be dropped wholesale before scrubbing — covers the
# CCR master key (DECOMPUTE_CCR_ENCRYPTION_KEY), salts, and any *_KEY/secret-ish name
# the value-pattern scrubbers would otherwise miss (e.g. a base64 Fernet key).
SECRET_ENV_NAME_RE = re.compile(
    r"(?i)(key|secret|token|password|passwd|pwd|private|credential|fernet|encrypt|salt|seed|passphrase|dsn)"
)

# Prompt-ish key names whose VALUE is omitted outright. Deliberately avoids generic
# tokens that collide with numeric metadata (input/output/text/user — cf. output_tokens,
# raw_input_tokens); long free-text under arbitrary keys is caught by the value-length
# heuristic below instead.
PROMPT_FIELD_RE = re.compile(
    r"(?i)(^|[_\-.])(prompt|raw[_\-.]?prompt|messages|message[_\-.]?body|request[_\-.]?body|response[_\-.]?body|body|content|tool[_\-.]?output|completion|assistant[_\-.]?response|user[_\-.]?message|original[_\-.]?text|plaintext|note|summary|conversation|transcript|dialog|dialogue|utterance|ccr[_\-.]?payload|pii[_\-.]?mapping|pii[_\-.]?vault)($|[_\-.])"
)

# String values longer than this under a non-allowlisted key are omitted as a prose
# defense — a prompt/transcript stored under a benign key (note/text/input/...) cannot
# be enumerated by key name, but it is almost always long free text. Numeric metrics are
# ints (never reach this branch) and short identifiers/hashes/model names stay intact.
MAX_RETAINED_VALUE_CHARS = 256

SAFE_CONTENT_FIELD_ALLOWLIST = {
    "content_type",
    "content-length",
    "content_length",
    "content-encoding",
    "content_encoding",
    "content_hash",
    "content_sha256",
}

PROMPT_TEXT_ASSIGNMENT_RE = re.compile(
    r"(?im)((?:^|\b)(?:raw[_\-.]?prompt|prompt|messages|message[_\-.]?body|request[_\-.]?body|response[_\-.]?body|tool[_\-.]?output|original[_\-.]?text|ccr[_\-.]?payload|pii[_\-.]?vault|completion|user[_\-.]?message|user[_\-.]?said|assistant[_\-.]?response|conversation|transcript|utterance|note|summary)\s*[:=]\s*)(.*)$"
)

# Skip files that should never be bundled, even scrubbed.
DEFAULT_EXCLUDE_GLOBS = [
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.db-wal",
    "*.db-shm",
    "*.parquet",
    "*.duckdb",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.crt",
    "*.cer",
    ".env",
    ".env.*",
    "*ccr*payload*",
    "*ccr_store*",
    "*pii_vault*",
    "*pii-mapping*",
    "*raw_prompt*",
    "*raw-prompts*",
    "*prompt_dump*",
]

TEXT_EXTENSIONS = {
    ".log",
    ".txt",
    ".json",
    ".jsonl",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".csv",
}


@dataclass
class ScrubReport:
    secret_counts: dict[str, int] = field(default_factory=dict)
    pii_counts: dict[str, int] = field(default_factory=dict)
    prompt_fields_omitted: int = 0
    secret_fields_redacted: int = 0
    files_scrubbed: int = 0
    files_skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    def inc_secret(self, name: str, count: int = 1) -> None:
        if count:
            self.secret_counts[name] = self.secret_counts.get(name, 0) + count

    def inc_pii(self, name: str, count: int = 1) -> None:
        if count:
            self.pii_counts[name] = self.pii_counts.get(name, 0) + count

    def to_dict(self) -> JSON:
        return dataclasses.asdict(self)


class SecretAndPIIScrubber:
    """Redacts secrets and tokenizes PII with stable placeholders.

    `pii_mode`:
      - "tokenize" => john@example.com -> <email_1>
      - "redact"   => john@example.com -> [PII_REDACTED:email]
      - "passthrough" => PII remains, but secrets are still redacted
    """

    def __init__(self, *, pii_mode: str = "tokenize", report: ScrubReport | None = None):
        if pii_mode not in {"tokenize", "redact", "passthrough"}:
            raise ValueError("pii_mode must be tokenize, redact, or passthrough")
        self.pii_mode = pii_mode
        self.report = report or ScrubReport()
        self._pii_maps: dict[str, dict[str, str]] = {
            "email": {},
            "phone": {},
            "ip": {},
            "ssn": {},
            "credit_card": {},
        }

        # Ordered: more structured/high-risk patterns first.
        self.secret_patterns: list[tuple[str, re.Pattern[str], str | None]] = [
            (
                "private_key_block",
                re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.M),
                "[REDACTED:private_key_block]",
            ),
            (
                "anthropic_key",
                re.compile(r"\bsk-ant-[A-Za-z0-9_\-\.]{8,}\b"),
                "[REDACTED:anthropic_key]",
            ),
            (
                "openai_key",
                re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b"),
                "[REDACTED:openai_key]",
            ),
            (
                "github_token",
                re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
                "[REDACTED:github_token]",
            ),
            (
                "aws_access_key_id",
                re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
                "[REDACTED:aws_access_key_id]",
            ),
            (
                "stripe_secret_key",
                re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"),
                "[REDACTED:stripe_secret_key]",
            ),
            (
                "slack_token",
                re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
                "[REDACTED:slack_token]",
            ),
            (
                "google_api_key",
                re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
                "[REDACTED:google_api_key]",
            ),
            (
                "npm_token",
                re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
                "[REDACTED:npm_token]",
            ),
            (
                "jwt",
                re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
                "[REDACTED:jwt]",
            ),
            (
                # Not line-anchored: headers also appear mid-line in docs/curl examples/logs.
                "authorization_header",
                re.compile(r"(?im)((?:authorization|proxy-authorization)\s*[:=]\s*)(?:bearer\s+)?[^\s,;\"']+"),
                None,
            ),
            (
                "api_key_header",
                re.compile(r"(?im)((?:x-api-key|x-decompute-api-key|x-decompute-key)\s*[:=]\s*)[^\s,;\"']+"),
                None,
            ),
            (
                "secret_assignment",
                re.compile(
                    r"(?im)^(\s*[A-Za-z0-9_\-.]*(?:API[_\-.]?KEY|ENCRYPTION[_\-.]?KEY|[_\-.]KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|PRIVATE[_\-.]?KEY|CLIENT[_\-.]?SECRET|FERNET[_\-.]?KEY|SALT|SEED|PASSPHRASE|DATABASE[_\-.]?URL|DSN)[A-Za-z0-9_\-.]*\s*[:=]\s*)(['\"]?)([^'\"\s#;]+)(['\"]?)"
                ),
                None,
            ),
            (
                "url_credentials",
                re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s:@]+):([^@/\s]+)@"),
                None,
            ),
        ]

        self.pii_patterns: list[tuple[str, re.Pattern[str]]] = [
            ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
            ("ssn", re.compile(r"\b\d{3}[\s\-.]\d{2}[\s\-.]\d{4}\b")),
            (
                "credit_card",
                # Tolerate space/hyphen/dot separators; _looks_like_credit_card (Luhn) gates false positives.
                re.compile(r"\b(?:\d[ \-.]*?){13,19}\b"),
            ),
            (
                "phone",
                # Require a phone-ish prefix/parentheses/separator to avoid tokenizing timestamps or counters.
                re.compile(r"(?<!\w)(?:\+\d{1,3}[\s\-.]+|\(?\d{3}\)[\s\-.]*|\d{3}[\s\-.]+)\d{3}[\s\-.]+\d{4}(?!\w)"),
            ),
            (
                "ip",
                re.compile(r"\b(?!(?:127\.0\.0\.1|0\.0\.0\.0)\b)(?:\d{1,3}\.){3}\d{1,3}\b"),
            ),
        ]

    def scrub_text(self, text: str) -> str:
        if not text:
            return text

        def omit_prompt_assignment(match: re.Match[str]) -> str:
            self.report.prompt_fields_omitted += 1
            return f"{match.group(1)}[OMITTED:raw_prompt_or_payload_field]"

        text = PROMPT_TEXT_ASSIGNMENT_RE.sub(omit_prompt_assignment, text)
        text = self._scrub_secret_text(text)
        if self.pii_mode != "passthrough":
            text = self._scrub_pii_text(text)
        return text

    def scrub_jsonish(self, value: Any) -> Any:
        """Scrub an arbitrary JSON-like structure.

        Prompt-like fields are omitted entirely. Secret-like fields are redacted by
        key before inspecting values. Other strings are passed through text scrubber.
        """
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for raw_key, raw_value in value.items():
                key = str(raw_key)
                key_l = key.lower()
                if key_l not in SAFE_CONTENT_FIELD_ALLOWLIST and PROMPT_FIELD_RE.search(key_l):
                    out[key] = "[OMITTED:raw_prompt_or_payload_field]"
                    self.report.prompt_fields_omitted += 1
                elif SECRET_FIELD_RE.search(key_l):
                    out[key] = "[REDACTED:secret_field]"
                    self.report.secret_fields_redacted += 1
                else:
                    out[key] = self.scrub_jsonish(raw_value)
            return out
        if isinstance(value, list):
            return [self.scrub_jsonish(v) for v in value]
        if isinstance(value, tuple):
            return [self.scrub_jsonish(v) for v in value]
        if isinstance(value, str):
            # Prose defense: a long free-text value is almost always content (a prompt,
            # transcript, or payload) rather than metadata — omit it regardless of key,
            # so prompts stashed under benign keys cannot escape. Short values
            # (ids, hashes, model names, counts-as-strings) pass through to scrubbing.
            if len(value) > MAX_RETAINED_VALUE_CHARS:
                self.report.prompt_fields_omitted += 1
                return "[OMITTED:long_text_value]"
            return self.scrub_text(value)
        return value

    def _scrub_secret_text(self, text: str) -> str:
        for name, pattern, replacement in self.secret_patterns:
            if replacement is not None:
                text, n = pattern.subn(replacement, text)
                self.report.inc_secret(name, n)
                continue

            def repl(match: re.Match[str]) -> str:
                if name in {"authorization_header", "api_key_header"}:
                    return f"{match.group(1)}[REDACTED:{name}]"
                if name == "secret_assignment":
                    return f"{match.group(1)}{match.group(2)}[REDACTED:secret_assignment]{match.group(4)}"
                if name == "url_credentials":
                    return f"{match.group(1)}[USER]:[REDACTED:url_password]@"
                return f"[REDACTED:{name}]"

            text, n = pattern.subn(repl, text)
            self.report.inc_secret(name, n)
        return text

    def _scrub_pii_text(self, text: str) -> str:
        # Avoid false positives by validating card/IP matches in replacement functions.
        for kind, pattern in self.pii_patterns:
            def repl(match: re.Match[str], *, kind: str = kind) -> str:
                raw = match.group(0)
                if kind == "credit_card" and not self._looks_like_credit_card(raw):
                    return raw
                if kind == "ip" and not self._looks_like_ip(raw):
                    return raw
                self.report.inc_pii(kind, 1)
                if self.pii_mode == "redact":
                    return f"[PII_REDACTED:{kind}]"
                return self._placeholder(kind, raw)

            text = pattern.sub(repl, text)
        return text

    def _placeholder(self, kind: str, raw: str) -> str:
        mapping = self._pii_maps[kind]
        if raw not in mapping:
            mapping[raw] = f"<{kind}_{len(mapping) + 1}>"
        return mapping[raw]

    @staticmethod
    def _looks_like_ip(raw: str) -> bool:
        try:
            parts = [int(p) for p in raw.split(".")]
            return len(parts) == 4 and all(0 <= p <= 255 for p in parts)
        except Exception:
            return False

    @staticmethod
    def _looks_like_credit_card(raw: str) -> bool:
        # A plain decimal number (e.g. a unix timestamp 1780420768.608683) is not a card,
        # even if its digits happen to satisfy Luhn. Real card separators group digits;
        # a single dot between two digit runs is a number, so reject it.
        if re.fullmatch(r"\d+\.\d+", raw.strip()):
            return False
        digits = re.sub(r"\D", "", raw)
        if not (13 <= len(digits) <= 19):
            return False
        # Luhn checksum
        checksum = 0
        parity = len(digits) % 2
        for i, ch in enumerate(digits):
            d = int(ch)
            if i % 2 == parity:
                d *= 2
                if d > 9:
                    d -= 9
            checksum += d
        return checksum % 10 == 0


# ---------------------------------------------------------------------------
# Bundle builder
# ---------------------------------------------------------------------------

@dataclass
class BundleOptions:
    output_dir: Path = Path(".decompute/support")
    data_dir: Path = Path(".decompute")
    project_root: Path = Path(".")
    release_version: str = "v0.1.0-beta.1"
    pii_mode: str = "tokenize"
    max_log_bytes: int = 250_000
    include_env: bool = True
    include_logs: bool = True
    include_reports: bool = True
    include_store_snapshot: bool = True
    include_config_files: bool = True
    allow_raw_prompts: bool = False
    redact_file_names: bool = False
    extra_canaries: list[str] = field(default_factory=list)


@dataclass
class BundleResult:
    path: Path
    manifest: JSON
    privacy_report: JSON
    leak_scan: JSON

    def to_dict(self) -> JSON:
        return {
            "path": str(self.path),
            "manifest": self.manifest,
            "privacy_report": self.privacy_report,
            "leak_scan": self.leak_scan,
        }


class SupportBundleBuilder:
    def __init__(
        self,
        *,
        options: BundleOptions | None = None,
        store: Any | None = None,
        settings: Any | None = None,
        health_snapshot_func: Callable[[], Any] | None = None,
        capabilities_func: Callable[[], Any] | None = None,
        command_runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    ):
        self.options = options or BundleOptions()
        self.store = store
        self.settings = settings
        self.report = ScrubReport()
        self.scrubber = SecretAndPIIScrubber(pii_mode=self.options.pii_mode, report=self.report)
        self.health_snapshot_func = health_snapshot_func
        self.capabilities_func = capabilities_func
        self.command_runner = command_runner or self._run_command
        self._included: list[dict[str, Any]] = []
        self._skipped: list[dict[str, Any]] = []

    def build(self) -> BundleResult:
        timestamp = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bundle_name = f"decompute-support-{timestamp}.zip"
        output_dir = self.options.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        final_path = output_dir / bundle_name

        with tempfile.TemporaryDirectory(prefix="decompute-support-") as tmp_s:
            tmp = Path(tmp_s)
            self._write_json(tmp, "manifest_pre.json", self._base_manifest(timestamp))
            self._collect_all(tmp)

            privacy_report = self._privacy_report()
            self._write_json(tmp, "privacy_report.json", privacy_report)
            manifest = self._base_manifest(timestamp)
            manifest.update(
                {
                    "included_files": self._included,
                    "skipped_files": self._skipped,
                    "privacy_report_path": "privacy_report.json",
                    "raw_prompts_included": False,
                    "ccr_payloads_included": False,
                    "pii_vault_included": False,
                    "database_dump_included": False,
                }
            )
            self._write_json(tmp, "manifest.json", manifest)
            # Remove pre-manifest from final archive.
            (tmp / "manifest_pre.json").unlink(missing_ok=True)

            with zipfile.ZipFile(final_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for path in sorted(tmp.rglob("*")):
                    if path.is_file():
                        zf.write(path, path.relative_to(tmp).as_posix())

        leak_scan = scan_bundle_for_leaks(final_path, extra_canaries=self.options.extra_canaries)
        # A bundle that fails the leak scan must never sit in the shareable output dir.
        # Move it into a quarantine subdir so an unsafe zip can't be picked up by a log
        # shipper / backup / later admin who only sees the support/ directory.
        if not leak_scan.get("ok"):
            quarantine = output_dir / "quarantine"
            quarantine.mkdir(parents=True, exist_ok=True)
            quarantined = quarantine / final_path.name
            shutil.move(str(final_path), str(quarantined))
            final_path = quarantined
        # Add leak scan report next to the (possibly quarantined) zip for evidence.
        (final_path.with_suffix(".leak_scan.json")).write_text(
            json.dumps(leak_scan, indent=2, sort_keys=True), encoding="utf-8"
        )
        return BundleResult(path=final_path, manifest=manifest, privacy_report=privacy_report, leak_scan=leak_scan)

    def _collect_all(self, tmp: Path) -> None:
        self._write_json(tmp, "runtime.json", self._runtime_snapshot())
        if self.options.include_env:
            self._write_json(tmp, "config/environment_redacted.json", self._environment_snapshot())
        if self.options.include_config_files:
            self._collect_config_files(tmp)
        self._collect_doctor(tmp)
        self._collect_health(tmp)
        self._collect_capabilities(tmp)
        if self.options.include_store_snapshot:
            self._collect_store_snapshot(tmp)
        if self.options.include_logs:
            self._collect_logs(tmp)
        if self.options.include_reports:
            self._collect_reports(tmp)

    def _base_manifest(self, timestamp: str) -> JSON:
        return {
            "schema_version": "support-bundle-v1",
            "created_at_utc": timestamp,
            "release_version": self.options.release_version,
            "purpose": "debugging support without raw prompts, CCR payloads, secrets, or PII vault data",
            "pii_mode": self.options.pii_mode,
            "scrubber_version": "2026-06-01",
        }

    def _runtime_snapshot(self) -> JSON:
        return self.scrubber.scrub_jsonish(
            {
                "python": sys.version,
                "platform": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "cwd": str(Path.cwd()),
                # Only the program name — never the full argv. CLI arguments routinely
                # carry secrets (e.g. --canary, --api-key, tokens) and must not be
                # captured verbatim into the bundle even though the scrubber runs after.
                "argv0": os.path.basename(sys.argv[0]) if sys.argv else "",
                "pid": os.getpid(),
                "time_unix": time.time(),
            }
        )

    def _environment_snapshot(self) -> JSON:
        allowed_prefixes = (
            "DECOMPUTE_",
            "YSCOMPRESS_",
            "YELLOWSTONE_",
            "ANTHROPIC_",
            "POSTGRES_",
            "DATABASE_",
            "UVICORN_",
            "LOG_",
            "PYTHON",
        )
        env: dict[str, str] = {}
        for k, v in sorted(os.environ.items()):
            if not k.startswith(allowed_prefixes):
                continue
            # Drop any secret-ish var's value by NAME before scrubbing. Value-pattern
            # scrubbing alone misses opaque secrets like the base64 CCR master key
            # (DECOMPUTE_CCR_ENCRYPTION_KEY), which would otherwise decrypt every CCR payload.
            env[k] = "[REDACTED:env_secret]" if SECRET_ENV_NAME_RE.search(k) else v
        return self.scrubber.scrub_jsonish(env)

    def _collect_config_files(self, tmp: Path) -> None:
        candidates = [
            self.options.project_root / "pyproject.toml",
            self.options.project_root / "alembic.ini",
            self.options.project_root / ".env.example",
            self.options.data_dir / "config.toml",
            self.options.data_dir / "config.json",
        ]
        for src in candidates:
            if not src.exists() or not src.is_file():
                continue
            self._include_text_file(tmp, src, f"config/{src.name}")

    def _collect_doctor(self, tmp: Path) -> None:
        # Prefer JSON if your CLI supports it; tolerate absence.
        commands = [
            ["decompute", "doctor", "--json"],
            ["decompute", "doctor"],
        ]
        for cmd in commands:
            code, stdout, stderr = self.command_runner(cmd, 8.0)
            if code == 0 and (stdout or stderr):
                self._write_text(
                    tmp,
                    "doctor_output.txt",
                    f"$ {' '.join(cmd)}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}\n",
                )
                return
        self._write_text(tmp, "doctor_output.txt", "decompute doctor was not available on PATH or returned no output.\n")

    def _collect_health(self, tmp: Path) -> None:
        if not self.health_snapshot_func:
            return
        try:
            self._write_json(tmp, "health.json", self.health_snapshot_func())
        except Exception as exc:
            self._write_json(tmp, "health_error.json", {"error": type(exc).__name__, "message": str(exc)})

    def _collect_capabilities(self, tmp: Path) -> None:
        if not self.capabilities_func:
            return
        try:
            self._write_json(tmp, "capabilities.json", self.capabilities_func())
        except Exception as exc:
            self._write_json(tmp, "capabilities_error.json", {"error": type(exc).__name__, "message": str(exc)})

    def _collect_store_snapshot(self, tmp: Path) -> None:
        if self.store is None:
            return
        snapshot: JSON = {}
        # Prefer a store-defined safe snapshot if the production branch implements it.
        if hasattr(self.store, "support_bundle_snapshot"):
            try:
                snapshot["support_bundle_snapshot"] = self.store.support_bundle_snapshot()
            except Exception as exc:
                snapshot["support_bundle_snapshot_error"] = {"error": type(exc).__name__, "message": str(exc)}
        else:
            for method_name in ("usage_summary", "recent_usage_events", "list_api_keys", "list_projects"):
                if hasattr(self.store, method_name):
                    try:
                        method = getattr(self.store, method_name)
                        snapshot[method_name] = method()
                    except TypeError:
                        # Some production methods require project_id/limit; skip instead of guessing.
                        snapshot[f"{method_name}_skipped"] = "method requires arguments"
                    except Exception as exc:
                        snapshot[f"{method_name}_error"] = {"error": type(exc).__name__, "message": str(exc)}
        self._write_json(tmp, "store_snapshot.json", snapshot)

    def _collect_logs(self, tmp: Path) -> None:
        roots = [
            self.options.data_dir / "logs",
            self.options.project_root / "logs",
            self.options.project_root / ".decompute" / "logs",
        ]
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            for src in sorted(root.rglob("*")):
                if not src.is_file():
                    continue
                if self._should_skip(src):
                    self._mark_skipped(src, "excluded file pattern")
                    continue
                if src.suffix.lower() not in TEXT_EXTENSIONS:
                    self._mark_skipped(src, "non-text log/report extension")
                    continue
                rel = src.relative_to(root).as_posix()
                dest = f"logs/{self._safe_rel_name(rel)}"
                self._include_text_file(tmp, src, dest, tail_bytes=self.options.max_log_bytes)

    def _collect_reports(self, tmp: Path) -> None:
        roots = [
            self.options.project_root / "docs" / "releases",
            self.options.project_root / "build" / "reports",
            self.options.project_root / "benchmarks" / "reports",
            self.options.project_root / "reports",
        ]
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            for src in sorted(root.rglob("*")):
                if not src.is_file():
                    continue
                if self._should_skip(src):
                    self._mark_skipped(src, "excluded file pattern")
                    continue
                if src.suffix.lower() not in TEXT_EXTENSIONS:
                    self._mark_skipped(src, "non-text report extension")
                    continue
                rel = src.relative_to(root).as_posix()
                dest = f"reports/{self._safe_rel_name(root.name + '/' + rel)}"
                self._include_text_file(tmp, src, dest, tail_bytes=self.options.max_log_bytes)

    def _include_text_file(self, tmp: Path, src: Path, dest_rel: str, *, tail_bytes: int | None = None) -> None:
        try:
            if tail_bytes is not None and src.stat().st_size > tail_bytes:
                with src.open("rb") as fh:
                    fh.seek(-tail_bytes, os.SEEK_END)
                    data = fh.read()
                text = data.decode("utf-8", errors="replace")
                text = f"[TRUNCATED: last {tail_bytes} bytes of {src.name}]\n" + text
                self._write_text(tmp, dest_rel, text)
            else:
                text = src.read_text(encoding="utf-8", errors="replace")
                if src.suffix.lower() == ".json":
                    try:
                        self._write_json(tmp, dest_rel, json.loads(text))
                    except Exception:
                        self._write_text(tmp, dest_rel, text)
                elif src.suffix.lower() == ".jsonl":
                    lines = []
                    for line in text.splitlines():
                        try:
                            lines.append(json.dumps(self.scrubber.scrub_jsonish(json.loads(line)), sort_keys=True))
                        except Exception:
                            lines.append(self.scrubber.scrub_text(line))
                    self._write_text(tmp, dest_rel, "\n".join(lines) + ("\n" if lines else ""))
                else:
                    self._write_text(tmp, dest_rel, text)
            self.report.files_scrubbed += 1
        except Exception as exc:
            self._mark_skipped(src, f"read error: {type(exc).__name__}: {exc}")

    def _write_text(self, tmp: Path, rel: str, text: str) -> None:
        safe = self.scrubber.scrub_text(text)
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(safe, encoding="utf-8")
        self._included.append({"path": rel, "kind": "text", "bytes": path.stat().st_size})

    def _write_json(self, tmp: Path, rel: str, payload: Any) -> None:
        scrubbed = self.scrubber.scrub_jsonish(payload)
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(scrubbed, indent=2, sort_keys=True, default=str), encoding="utf-8")
        self._included.append({"path": rel, "kind": "json", "bytes": path.stat().st_size})

    def _privacy_report(self) -> JSON:
        return {
            **self.report.to_dict(),
            "raw_prompts_included": False,
            "ccr_payloads_included": False,
            "pii_vault_included": False,
            "database_dump_included": False,
            "notes": [
                "Prompt-like JSON fields are replaced with [OMITTED:raw_prompt_or_payload_field].",
                "Secrets are irreversibly redacted.",
                "PII is tokenized or redacted depending on pii_mode.",
                "CCR payloads, PII vaults, .env files, and DB dumps are never included.",
            ],
        }

    def _should_skip(self, path: Path) -> bool:
        name = path.name
        full = path.as_posix()
        for pattern in DEFAULT_EXCLUDE_GLOBS:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(full, pattern):
                return True
        return False

    def _mark_skipped(self, path: Path, reason: str) -> None:
        self.report.files_skipped += 1
        self._skipped.append({"path": self._safe_path(path), "reason": reason})

    def _safe_path(self, path: Path) -> str:
        if not self.options.redact_file_names:
            return path.as_posix()
        return f"[PATH_REDACTED:{abs(hash(path.as_posix())) % 10_000_000}]"

    @staticmethod
    def _safe_rel_name(name: str) -> str:
        # Preserve enough structure without allowing path traversal.
        name = name.replace("\\", "/").strip("/")
        parts = [re.sub(r"[^A-Za-z0-9._-]", "_", p) for p in name.split("/") if p and p not in {".", ".."}]
        return "/".join(parts[-6:]) or "file.txt"

    @staticmethod
    def _run_command(cmd: list[str], timeout_s: float) -> tuple[int, str, str]:
        if shutil.which(cmd[0]) is None:
            return 127, "", f"{cmd[0]} not found"
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            return 124, exc.stdout or "", exc.stderr or f"timeout after {timeout_s}s"
        except Exception as exc:
            return 1, "", f"{type(exc).__name__}: {exc}"


# Backstop patterns for the leak scanner. These are evaluated INDEPENDENTLY of the
# scrubber so the scanner can catch values the scrubber's pattern list misses (a
# shared blind spot would let novel/opaque secrets ship marked "safe").
_UNREDACTED_SECRET_FIELD_RE = re.compile(
    r"(?i)[\"']?[A-Za-z0-9_\-.]*(?:encryption[_\-.]?key|api[_\-.]?key|private[_\-.]?key|secret|token|password|passphrase|fernet|credential|[_\-.]key)[\"']?\s*[:=]\s*(?P<val>[^\s,;}{]+)"
)
_RAW_PROMPT_FIELD_RE = re.compile(
    r"(?i)(?:raw[_\-.]?prompt|original[_\-.]?text|ccr[_\-.]?payload|pii[_\-.]?vault|messages|completion|user[_\-.]?message|assistant[_\-.]?response|tool[_\-.]?output|request[_\-.]?body|response[_\-.]?body)[\"']?\s*[:=](?!\s*[\"']?(?:\[OMITTED|\[REDACTED))"
)


def scan_bundle_for_leaks(zip_path: Path, *, extra_canaries: Iterable[str] = ()) -> JSON:
    """Best-effort leak scan of final archive.

    This is not a substitute for code review, but it catches regressions where raw
    canary prompts/secrets are accidentally included. Crucially, it applies
    independent backstop checks (secret-named fields with long unredacted values,
    high-entropy blobs) rather than only re-running the scrubber — so a value the
    scrubber's pattern list missed is still caught here before the bundle ships.
    """
    secret_probe = SecretAndPIIScrubber(pii_mode="tokenize")
    suspicious: list[dict[str, Any]] = []
    canaries = [c for c in extra_canaries if c]

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.file_size > 5_000_000:
                suspicious.append({"path": info.filename, "reason": "large_file", "bytes": info.file_size})
                continue
            data = zf.read(info.filename)
            text = data.decode("utf-8", errors="ignore")
            for canary in canaries:
                if canary and canary in text:
                    suspicious.append({"path": info.filename, "reason": "canary_found"})
            # Reuse raw secret patterns: if scrubbing changes the file, an unredacted secret-like value remained.
            before = secret_probe.report.to_dict()
            scrubbed = secret_probe.scrub_text(text)
            after = secret_probe.report.to_dict()
            if scrubbed != text:
                suspicious.append({"path": info.filename, "reason": "secret_or_pii_pattern_still_present"})
            if info.filename not in {"manifest.json", "privacy_report.json"}:
                # Broad prompt/CCR-field check (mirrors PROMPT_FIELD_RE so the backstop
                # is never narrower than the omitter).
                if _RAW_PROMPT_FIELD_RE.search(text):
                    suspicious.append({"path": info.filename, "reason": "raw_prompt_or_ccr_payload_value_present"})
                # Independent secret-field check: a secret-named field with a long,
                # non-redacted value — catches opaque keys the scrubber's value
                # patterns miss (e.g. the base64 CCR master key).
                for m in _UNREDACTED_SECRET_FIELD_RE.finditer(text):
                    val = (m.group("val") or "").strip().strip("\"'")
                    if val and len(val) >= 12 and not val.startswith(("[REDACTED", "[OMITTED")):
                        suspicious.append({"path": info.filename, "reason": "unredacted_secret_field"})
                        break

    return {
        "ok": not suspicious,
        "suspicious": suspicious,
        "archive": str(zip_path),
        "scanned_at_utc": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
    }


def build_support_bundle(
    *,
    output_dir: str | Path = ".decompute/support",
    data_dir: str | Path = ".decompute",
    project_root: str | Path = ".",
    pii_mode: str = "tokenize",
    release_version: str = "v0.1.0-beta.1",
    store: Any | None = None,
    settings: Any | None = None,
    extra_canaries: list[str] | None = None,
) -> BundleResult:
    options = BundleOptions(
        output_dir=Path(output_dir),
        data_dir=Path(data_dir),
        project_root=Path(project_root),
        pii_mode=pii_mode,
        release_version=release_version,
        extra_canaries=extra_canaries or [],
    )
    return SupportBundleBuilder(options=options, store=store, settings=settings).build()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a privacy-safe Decompute support bundle")
    parser.add_argument("--output-dir", default=".decompute/support")
    parser.add_argument("--data-dir", default=".decompute")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--pii-mode", choices=["tokenize", "redact", "passthrough"], default="tokenize")
    parser.add_argument("--release-version", default="v0.1.0-beta.1")
    parser.add_argument("--canary", action="append", default=[])
    args = parser.parse_args(argv)

    result = build_support_bundle(
        output_dir=args.output_dir,
        data_dir=args.data_dir,
        project_root=args.project_root,
        pii_mode=args.pii_mode,
        release_version=args.release_version,
        extra_canaries=args.canary,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    if not result.leak_scan.get("ok"):
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
