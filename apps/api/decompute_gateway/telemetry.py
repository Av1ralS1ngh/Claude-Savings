"""Anonymous, COUNTS-ONLY savings telemetry.

Sends a WEEKLY rollup of savings counts to the telemetry Worker so we can see aggregate token savings
across users. This module is the ONLY thing that ever phones home, and it is deliberately tiny and
auditable.

PRIVACY CONTRACT (do not weaken):
  - The payload contains ONLY counts: requests, tokens_saved, tokens_before, est_savings_usd, plus the
    random device id, the OS family, and the gateway version. It is built from a fixed allowlist of
    keys; _assert_counts_only() raises if any non-allowlisted (e.g. content-shaped) key sneaks in.
  - NEVER sent: prompts, messages, file contents, model inputs, request bodies, IPs, or any user text.
  - The device id is the random UUID from ~/.decompute/device.id (no hardware identifiers); the Worker
    stores only its sha256.
  - Off-switches honored upstream (settings.telemetry_enabled): DECOMPUTE_TELEMETRY=off, DO_NOT_TRACK=1,
    or `decompute telemetry off` (config.toml [telemetry].enabled=false, read here too).
  - Fire-and-forget on a background thread; never blocks or fails a request, never raises into callers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import sys
import threading
import time
import tomllib
import uuid

log = logging.getLogger("decompute.telemetry")

# The COMPLETE set of keys allowed in a telemetry payload. Anything outside this is a bug and is caught
# by _assert_counts_only before the request leaves the process.
_ALLOWED_KEYS = frozenset({"device", "week", "requests", "tokens_saved", "tokens_before", "est_savings_usd", "os", "version"})
# Keys that would indicate user content leaked in — an extra tripwire on top of the allowlist.
_FORBIDDEN_SUBSTRINGS = ("prompt", "message", "content", "text", "file", "input", "system", "body", "model")


def _assert_counts_only(payload: dict) -> None:
    """Raise if the payload contains anything other than the allowlisted COUNT fields. This is the
    last line of defense that makes 'we never send prompts' a code-enforced guarantee, not a promise."""
    for k in payload:
        kl = str(k).lower()
        if k not in _ALLOWED_KEYS or any(bad in kl for bad in _FORBIDDEN_SUBSTRINGS):
            raise ValueError(f"telemetry payload contains a non-count field: {k!r}")
        v = payload[k]
        if not isinstance(v, (str, int, float)):
            raise ValueError(f"telemetry payload field {k!r} is not a scalar count/string: {type(v)}")


def _iso_week(now: datetime) -> str:
    y, w, _ = now.isocalendar()
    return f"{y:04d}-W{w:02d}"


def _state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "telemetry.json"


def _load_last_sent(data_dir: Path) -> float:
    try:
        return float(json.loads(_state_path(data_dir).read_text(encoding="utf-8")).get("last_sent_epoch", 0))
    except Exception:
        return 0.0


def _save_last_sent(data_dir: Path, epoch: float) -> None:
    try:
        _state_path(data_dir).write_text(json.dumps({"last_sent_epoch": epoch}), encoding="utf-8")
    except OSError:
        pass


def _device_id(data_dir: Path) -> str:
    """The random per-device UUID (no hardware info). Reuses ~/.decompute/device.id if the CLI made one;
    otherwise mints + persists one. Matches the CLI's format ('dev-' + uuid hex)."""
    p = Path(data_dir) / "device.id"
    try:
        existing = p.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    did = "dev-" + uuid.uuid4().hex
    try:
        p.write_text(did + "\n", encoding="utf-8")
        p.chmod(0o600)
    except OSError:
        pass
    return did


def _config_opt_out(data_dir: Path) -> bool:
    """`decompute telemetry off` writes [telemetry].enabled=false to config.toml — honor it here too, so
    the CLI toggle works even though the gateway is a separate process."""
    try:
        cfg = tomllib.loads((Path(data_dir) / "config.toml").read_text(encoding="utf-8"))
        val = (cfg.get("telemetry") or {}).get("enabled")
        return val is False
    except Exception:
        return False


def build_payload(summary: dict, *, device: str, version: str, now: datetime) -> dict:
    """Construct the COUNTS-ONLY payload from a usage_summary() dict. Reads only count fields by name —
    it cannot accidentally include content because usage_summary never holds any."""
    payload = {
        "device": device,
        "week": _iso_week(now),
        "requests": int(summary.get("requests") or 0),
        "tokens_saved": int(summary.get("tokens_saved") or 0),
        "tokens_before": int(summary.get("raw_input_tokens") or 0),
        "est_savings_usd": round(float(summary.get("estimated_savings_usd") or 0.0), 4),
        "os": sys.platform,
        "version": str(version or ""),
    }
    _assert_counts_only(payload)  # enforce the privacy contract before anyone can send it
    return payload


def _post(url: str, payload: dict, *, timeout: float = 5.0) -> bool:
    import httpx
    try:
        r = httpx.post(url.rstrip("/") + "/v1/savings", json=payload, timeout=timeout)
        return r.status_code < 400
    except Exception as exc:
        log.debug("telemetry post failed (ignored)", extra={"extra": {"error": str(exc)}})
        return False


def maybe_send_weekly(settings, store, *, version: str, force: bool = False) -> None:
    """If telemetry is enabled and it's been >= interval since the last send, beacon this week's savings.
    Runs the network call on a daemon thread so it NEVER blocks startup or a request. Swallows all errors
    — telemetry must never affect the gateway's behavior."""
    if not getattr(settings, "telemetry_enabled", False):
        return
    data_dir = Path(settings.data_dir)
    if _config_opt_out(data_dir):  # the CLI `telemetry off` toggle
        return
    now = time.time()
    interval_s = max(1, int(getattr(settings, "telemetry_interval_days", 7))) * 86400
    if not force and (now - _load_last_sent(data_dir)) < interval_s:
        return

    def _run() -> None:
        try:
            summary = store.usage_summary(project_id="proj_default", since=None)
            if not int(summary.get("requests") or 0):
                _save_last_sent(data_dir, now)  # nothing to report yet; don't recompute every startup
                return
            payload = build_payload(summary, device=_device_id(data_dir), version=version, now=datetime.now(timezone.utc))
            if _post(settings.telemetry_url, payload):
                _save_last_sent(data_dir, now)
        except Exception as exc:
            log.debug("telemetry skipped (ignored)", extra={"extra": {"error": str(exc)}})

    threading.Thread(target=_run, name="decompute-telemetry", daemon=True).start()


def start_periodic(settings, store, *, version: str) -> None:
    """Beacon at boot, then re-check once a day so a gateway that stays up for weeks (launchd / Task
    Scheduler keep it running) still sends its weekly rollup without needing a restart. maybe_send_weekly
    self-gates on the interval and the opt-outs, so the daily tick is cheap and a no-op most days."""
    maybe_send_weekly(settings, store, version=version)

    def _loop() -> None:
        while True:
            time.sleep(86400)  # once a day; the weekly cadence is enforced inside maybe_send_weekly
            try:
                maybe_send_weekly(settings, store, version=version)
            except Exception:
                pass

    threading.Thread(target=_loop, name="decompute-telemetry-loop", daemon=True).start()
