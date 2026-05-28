from __future__ import annotations

import os
from typing import Any

import httpx


class DecomputeError(RuntimeError):
    pass


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


class Decompute:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: float = 120.0):
        self.api_key = api_key or os.getenv("DECOMPUTE_API_KEY", "dc_dev_local")
        self.base_url = (base_url or os.getenv("DECOMPUTE_BASE_URL", "http://127.0.0.1:8080")).rstrip("/")
        self.timeout = timeout
        self.devices = Devices(self)
        self.compression = Compression(self)
        self.routes = Routes(self)
        self.deployments = Deployments(self)
        self.messages = Messages(self)
        self.chat = Chat(self)
        self.api_keys = ApiKeys(self)

    def headers(self) -> dict[str, str]:
        return {"x-decompute-key": self.api_key, "content-type": "application/json"}

    def get(self, path: str) -> Any:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(self.base_url + path, headers=self.headers())
        return self._handle(resp)

    def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(self.base_url + path, headers=self.headers(), json=json or {})
        return self._handle(resp)

    def _handle(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            raise DecomputeError(f"HTTP {resp.status_code}: {resp.text}")
        if not resp.content:
            return None
        return resp.json()
