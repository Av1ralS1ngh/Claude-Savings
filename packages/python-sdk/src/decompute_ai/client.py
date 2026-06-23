from __future__ import annotations

import os
from typing import Any

import httpx

from .config import base_url as cfg_base_url, load_claude_key, load_device_id
from .errors import DecomputeError, error_for_status

__all__ = ["Decompute", "DecomputeError"]


class _Resource:
    def __init__(self, client: "Decompute"):
        self._client = client


class Devices(_Resource):
    def scan(self) -> list[dict[str, Any]]:
        return self._client.post("/v1/devices/scan")

    def list(self) -> list[dict[str, Any]]:
        return self._client.get("/v1/devices")


class Compression(_Resource):
    def preview(self, messages: list[dict[str, Any]], mode: str = "safe", ccr: bool = True) -> dict[str, Any]:
        return self._client.post("/v1/compression/preview", {"messages": messages, "mode": mode, "ccr": ccr})


class Routes(_Resource):
    def plan(self, model: str, messages: list[dict[str, Any]], yscompress: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._client.post("/v1/routes/plan", {"model": model, "messages": messages, "yscompress": yscompress or {}})


class Deployments(_Resource):
    def create(self, name: str, plan: dict[str, Any]) -> dict[str, Any]:
        return self._client.post("/v1/deployments", {"name": name, "plan": plan})

    def list(self) -> list[dict[str, Any]]:
        return self._client.get("/v1/deployments")


class Messages(_Resource):
    def create(self, model: str, messages: list[dict[str, Any]], max_tokens: int = 1024, **kwargs: Any) -> dict[str, Any]:
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        body.update(kwargs)
        return self._client.post("/v1/messages", body)


class ChatCompletions(_Resource):
    def create(self, model: str, messages: list[dict[str, Any]], max_tokens: int = 1024, **kwargs: Any) -> dict[str, Any]:
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        body.update(kwargs)
        return self._client.post("/v1/chat/completions", body)


class Chat(_Resource):
    def __init__(self, client: "Decompute"):
        super().__init__(client)
        self.completions = ChatCompletions(client)


class ApiKeys(_Resource):
    def create(self, name: str = "default", project_id: str = "proj_default") -> dict[str, Any]:
        return self._client.post("/v1/api_keys", {"name": name, "project_id": project_id})

    def list(self) -> list[dict[str, Any]]:
        return self._client.get("/v1/api_keys")

    def revoke(self, key_id: str) -> dict[str, Any]:
        return self._client.post(f"/v1/api_keys/{key_id}/revoke")


class Models(_Resource):
    def list(self) -> dict[str, Any]:
        return self._client.get("/v1/models")


class Licenses(_Resource):
    """Device-licensing (BYOK axis): redeem a paid checkout, activate a key, read status."""
    def redeem(self, session_id: str) -> dict[str, Any]:
        return self._client.post("/v1/billing/redeem-checkout", {"session_id": session_id})

    def activate(self, license_key: str) -> dict[str, Any]:
        return self._client.post("/v1/licenses/activate", {"license_key": license_key})

    def status(self) -> dict[str, Any]:
        return self._client.get("/v1/licenses/status")

    def deactivate(self) -> dict[str, Any]:
        return self._client.post("/v1/licenses/deactivate")


class TeacherStudent(_Resource):
    """Command-gated local coding student. Backs `decompute ts-run` / `ts-health` / `local-chat`."""

    def health(self) -> dict[str, Any]:
        return self._client.get("/v1/teacher_student/health")

    def run(
        self,
        task: str,
        *,
        tests: str | None = None,
        require_teacher: bool = False,
        execute_tests: bool = False,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        source: str = "cli",
        command: str = "ts-run",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "task": task,
            "mode": "student_first",
            "require_teacher": require_teacher,
            "execute_tests": execute_tests,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "source": source,
            "command": command,
        }
        if tests is not None:
            body["tests"] = tests
        return self._client.post("/v1/teacher_student/run", body)

    def local_chat(self, prompt: str, *, max_tokens: int = 2048, temperature: float = 0.2) -> dict[str, Any]:
        return self._client.post(
            "/v1/teacher_student/local-chat",
            {"prompt": prompt, "max_tokens": max_tokens, "temperature": temperature, "source": "cli", "command": "local-chat"},
        )

    def summary(self) -> dict[str, Any]:
        return self._client.get("/v1/teacher_student/summary")

    def export_sft(self) -> Any:
        """Download the SFT distillation export (NDJSON). Requires the distill:read scope."""
        return self._client.get("/v1/teacher_student/export/sft.jsonl", raw=True)


class Usage(_Resource):
    def summary(self) -> dict[str, Any]:
        return self._client.get("/v1/usage")

    def export(self, *, format: str = "json", start: str | None = None, end: str | None = None) -> Any:
        params: dict[str, str] = {"format": format}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        return self._client.get("/v1/usage/export", params=params, raw=(format == "csv"))


class Decompute:
    """Thin client for the Decompute Gateway (BYOK — bring your own Claude key).

    Credential resolution: explicit ``api_key`` argument > ``DECOMPUTE_CLAUDE_KEY`` /
    ``ANTHROPIC_API_KEY`` env > the saved key file (``~/.decompute/claude.key``, written
    by ``decompute claude``). When no key is found the client sends no auth header and the
    local gateway serves it as ``local_noauth``. Base URL: explicit > ``DECOMPUTE_BASE_URL``
    env > ``config.toml`` ([gateway].base_url) > local default.

    A custom httpx ``transport`` may be injected for in-process testing.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        *,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ):
        # BYOK: the credential is the user's own Claude/Anthropic key (sent as x-api-key).
        self.api_key = api_key or load_claude_key() or ""
        self.device_id = load_device_id()
        self.base_url = (base_url or os.getenv("DECOMPUTE_BASE_URL") or cfg_base_url()).rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._transport = transport
        self.devices = Devices(self)
        self.compression = Compression(self)
        self.routes = Routes(self)
        self.deployments = Deployments(self)
        self.messages = Messages(self)
        self.chat = Chat(self)
        self.api_keys = ApiKeys(self)
        self.models = Models(self)
        self.usage = Usage(self)
        self.licenses = Licenses(self)
        self.teacher_student = TeacherStudent(self)

    def headers(self) -> dict[str, str]:
        # BYOK: forward the user's own Claude key as x-api-key. When none is set we send
        # no auth header and the gateway falls back to local_noauth (proj_default/admin).
        h = {"content-type": "application/json"}
        if self.api_key:
            h["x-api-key"] = self.api_key
        # Device-licensing identity: the gateway HMAC-hashes this to look up Pro/quota.
        if self.device_id:
            h["x-decompute-device"] = self.device_id
        return h

    def _new_client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=self._transport)

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None, params: dict[str, str] | None = None, raw: bool = False) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self._new_client() as client:
                    resp = client.request(method, path, headers=self.headers(), json=json, params=params)
                # Retry only safe, transient upstream failures.
                if resp.status_code in (502, 503, 504, 529) and attempt < self.max_retries:
                    continue
                return self._handle(resp, raw=raw)
            except httpx.TransportError as exc:  # connect/read timeouts — retry
                last_exc = exc
                if attempt >= self.max_retries:
                    raise DecomputeError(f"connection to {self.base_url} failed: {exc}") from exc
        raise DecomputeError(str(last_exc) if last_exc else "request failed")

    def get(self, path: str, params: dict[str, str] | None = None, raw: bool = False) -> Any:
        return self._request("GET", path, params=params, raw=raw)

    def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json=json or {})

    def _handle(self, resp: httpx.Response, *, raw: bool = False) -> Any:
        if resp.status_code >= 400:
            try:
                body = resp.json()
                message = (body.get("error", {}) or {}).get("message") if isinstance(body, dict) else None
                message = message or (body.get("detail") if isinstance(body, dict) else None) or resp.text
            except Exception:
                body, message = None, resp.text
            raise error_for_status(resp.status_code, f"HTTP {resp.status_code}: {message}", body)
        if raw:
            return resp.text
        if not resp.content:
            return None
        return resp.json()
