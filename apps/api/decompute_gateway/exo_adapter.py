"""exo bring-your-own cluster adapter (V1c).

exo already exposes the OpenAI-compatible `/v1/chat/completions` endpoint that
`LocalStudentProvider` uses. This module adds the cluster extension: read `/state`,
normalize topology/node metadata, and report the experimental serve gate without
owning exo orchestration.
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

DEFAULT_EXO_BASE_URL = "http://127.0.0.1:52415/v1"
EXO_BACKEND = "exo"


def exo_root_url(base_url: str) -> str:
    """Return the exo API root for a configured `/v1` base URL."""
    parsed = urlparse((base_url or DEFAULT_EXO_BASE_URL).rstrip("/"))
    path = parsed.path.rstrip("/")
    if path == "/v1":
        path = ""
    elif path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    root = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    return root.rstrip("/")


def extract_model_ids(payload: Any) -> list[str]:
    """Normalize exo `/models` or OpenAI `/v1/models` payloads into model ids."""
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("models") or payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            mid = item
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("model_id") or item.get("modelId") or item.get("name")
        else:
            mid = None
        if mid and str(mid) not in ids:
            ids.append(str(mid))
    return ids


def summarize_exo_state(state: Any) -> dict[str, Any]:
    """Return a stable, privacy-safe ClusterCapability summary from exo `/state`.

    The live exo state is a Pydantic object serialized by FastAPI. Field aliases may
    appear as camelCase or snake_case, so this parser accepts both and degrades to
    counts/ids instead of failing on minor schema drift.
    """
    if not isinstance(state, dict):
        return {"ok": False, "error": "bad_state"}

    identities = _mapping(state, "nodeIdentities", "node_identities")
    memories = _mapping(state, "nodeMemory", "node_memory")
    networks = _mapping(state, "nodeNetwork", "node_network")
    thunderbolt = _mapping(state, "nodeThunderbolt", "node_thunderbolt")
    backends = _mapping(state, "nodeBackends", "node_backends")
    last_seen = _mapping(state, "lastSeen", "last_seen")
    topology = _mapping(state, "topology")

    node_ids: set[str] = set()
    for m in (identities, memories, networks, thunderbolt, backends, last_seen):
        node_ids.update(str(k) for k in m.keys())
    for n in _list(topology, "nodes"):
        node_ids.add(str(n))

    nodes: list[dict[str, Any]] = []
    total_ram = 0
    available_ram = 0
    for node_id in sorted(node_ids):
        identity = _as_dict(identities.get(node_id))
        memory = _as_dict(memories.get(node_id))
        network = _as_dict(networks.get(node_id))
        tb = _as_dict(thunderbolt.get(node_id))
        node_backends = backends.get(node_id) or []
        ram_total = _memory_bytes(_pick(memory, "ramTotal", "ram_total"))
        ram_available = _memory_bytes(_pick(memory, "ramAvailable", "ram_available"))
        total_ram += ram_total or 0
        available_ram += ram_available or 0
        interfaces = [_as_dict(x) for x in _list(network, "interfaces")]
        network_types = sorted(
            {
                str(_pick(i, "interfaceType", "interface_type") or "unknown")
                for i in interfaces
            }
        )
        tb_interfaces = _list(tb, "interfaces")
        nodes.append(
            {
                "id": node_id,
                "friendly_name": _pick(identity, "friendlyName", "friendly_name"),
                "model_id": _pick(identity, "modelId", "model_id"),
                "chip_id": _pick(identity, "chipId", "chip_id"),
                "backends": [str(x) for x in node_backends] if isinstance(node_backends, list) else [],
                "ram_total_gb": _gb(ram_total),
                "ram_available_gb": _gb(ram_available),
                "network_interfaces": len(interfaces),
                "network_types": network_types,
                "thunderbolt_interfaces": len(tb_interfaces),
                "last_seen": str(last_seen.get(node_id)) if node_id in last_seen else None,
            }
        )

    instances_raw = _mapping(state, "instances")
    instances: list[dict[str, Any]] = []
    active_models: set[str] = set()
    for key, value in instances_raw.items():
        instance = _as_dict(value)
        shard_assignments = _as_dict(_pick(instance, "shardAssignments", "shard_assignments"))
        model_id = (
            _pick(shard_assignments, "modelId", "model_id")
            or _model_from_runner_shards(shard_assignments)
        )
        if model_id:
            active_models.add(str(model_id))
        instances.append(
            {
                "id": str(_pick(instance, "instanceId", "instance_id") or key),
                "model": str(model_id) if model_id else None,
                "kind": _pick(instance, "type", "kind", "tag"),
                "runner_count": len(_mapping(shard_assignments, "runnerToShard", "runner_to_shard")),
            }
        )

    topology_summary = _summarize_topology(topology)
    network_types = sorted({t for node in nodes for t in (node.get("network_types") or [])})
    return {
        "ok": True,
        "node_count": len(nodes),
        "nodes": nodes,
        "pooled_ram_gb": _gb(total_ram),
        "available_ram_gb": _gb(available_ram),
        "instance_count": len(instances),
        "instances": instances,
        "active_models": sorted(active_models),
        "topology": topology_summary,
        "network": {"types": network_types},
        "thunderbolt_bridge_cycles": _list(state, "thunderboltBridgeCycles", "thunderbolt_bridge_cycles"),
        "state_keys": sorted(str(k) for k in state.keys()),
    }


def assess_exo_cluster(
    summary: dict[str, Any],
    *,
    requested_model: str | None = None,
    models_loaded: list[str] | None = None,
    measured_ok: bool | None = None,
    tokens_per_s: float | None = None,
    ttft_ms: int | None = None,
) -> dict[str, Any]:
    """Opinionated readiness verdict for the BYO exo cluster.

    This is intentionally a pre-integration guardrail, not a benchmark replacement:
    it catches obvious issues (no nodes, WiFi-only, no topology edges, model not
    downloaded/active) and records the measured canary when the caller has one.
    """
    warnings: list[str] = []
    blockers: list[str] = []
    models_loaded = models_loaded or []
    active_models = [str(x) for x in (summary.get("active_models") or [])]
    node_count = int(summary.get("node_count") or 0)
    topology = _as_dict(summary.get("topology"))
    edge_count = int(topology.get("edge_count") or 0)
    interconnect = _interconnect_verdict(summary)

    if not summary.get("ok", False):
        blockers.append(str(summary.get("error") or "state_unavailable"))
    if node_count <= 0:
        blockers.append("no_nodes")
    elif node_count == 1:
        warnings.append("single_node_exo_cluster")
    if node_count > 1 and edge_count == 0:
        warnings.append("no_topology_edges")
    if interconnect["tier"] == "unsupported":
        blockers.append("wifi_interconnect_unsupported")
    elif interconnect["tier"] == "unknown":
        warnings.append("unknown_interconnect")

    model_info: dict[str, Any] = {}
    if requested_model:
        downloaded = _model_list_contains(models_loaded, requested_model)
        active = _model_list_contains(active_models, requested_model)
        model_info = {
            "requested": requested_model,
            "downloaded": downloaded,
            "active": active,
        }
        if models_loaded and not downloaded:
            warnings.append("requested_model_not_listed")
        if not active:
            warnings.append("requested_model_not_active")

    if measured_ok is False:
        blockers.append("canary_failed")

    return {
        "ok": not blockers,
        "phase": "eval_only",
        "blockers": _dedupe(blockers),
        "warnings": _dedupe(warnings),
        "node_count": node_count,
        "interconnect": interconnect,
        "model": model_info,
        "measured_ok": measured_ok,
        "tokens_per_s": tokens_per_s,
        "ttft_ms": ttft_ms,
        "recommended_next": _recommended_next(blockers, warnings, measured_ok=measured_ok),
    }


async def exo_cluster_capability(
    *,
    base_url: str = DEFAULT_EXO_BASE_URL,
    api_key: str | None = None,
    timeout_s: float = 8.0,
    requested_model: str | None = None,
    models_loaded: list[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Fetch and normalize exo `/state`. Never raises."""
    root = exo_root_url(base_url)
    headers: dict[str, str] = {}
    if api_key and api_key.lower() != "none":
        headers["authorization"] = f"Bearer {api_key}"
    try:
        timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 3.0))
        async with httpx.AsyncClient(base_url=root, timeout=timeout, transport=transport) as client:
            resp = await asyncio.wait_for(client.get("/state", headers=headers), timeout=timeout_s)
        if resp.status_code >= 400:
            return _exo_error(root, "state_http_error", status_code=resp.status_code)
        summary = summarize_exo_state(_safe_json(resp))
        summary.update(
            {
                "backend": EXO_BACKEND,
                "root_url": root,
                "base_url": base_url.rstrip("/"),
                "experimental": True,
                "owned_by_decompute": False,
                "api_formats": {
                    "openai_chat_completions": "/v1/chat/completions",
                    "claude_messages": "/v1/messages",
                    "openai_responses": "/v1/responses",
                    "ollama": "/ollama/api/chat",
                },
            }
        )
        summary["readiness"] = assess_exo_cluster(
            summary,
            requested_model=requested_model,
            models_loaded=models_loaded,
        )
        return summary
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        return _exo_error(root, "state_unreachable", error_detail=type(exc).__name__)


def _exo_error(root: str, error: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "backend": EXO_BACKEND,
        "root_url": root,
        "experimental": True,
        "owned_by_decompute": False,
        "error": error,
        **extra,
    }


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _mapping(obj: Any, *keys: str) -> dict[str, Any]:
    val = _pick(_as_dict(obj), *keys) if keys else obj
    return val if isinstance(val, dict) else {}


def _list(obj: Any, *keys: str) -> list[Any]:
    val = _pick(_as_dict(obj), *keys) if keys else obj
    if isinstance(val, list):
        return val
    if isinstance(val, tuple):
        return list(val)
    return []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pick(obj: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in obj:
            return obj[key]
    return None


def _memory_bytes(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, dict):
        raw = _pick(value, "inBytes", "in_bytes", "bytes")
        return _memory_bytes(raw)
    return None


def _gb(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / (1024 ** 3), 2)


def _model_from_runner_shards(shard_assignments: dict[str, Any]) -> str | None:
    runners = _mapping(shard_assignments, "runnerToShard", "runner_to_shard")
    for shard in runners.values():
        shard_d = _as_dict(shard)
        card = _as_dict(_pick(shard_d, "modelCard", "model_card"))
        mid = _pick(card, "id", "modelId", "model_id")
        if mid:
            return str(mid)
    return None


def _interconnect_verdict(summary: dict[str, Any]) -> dict[str, Any]:
    network_types = {str(x).lower() for x in (_as_dict(summary.get("network")).get("types") or [])}
    topology = _as_dict(summary.get("topology"))
    connection_types = {str(x).lower() for x in (topology.get("connection_types") or [])}
    labels = network_types | connection_types
    if any("thunderbolt" in x or "rdma" in x or "jaccl" in x for x in labels):
        return {"tier": "fast", "label": "thunderbolt_or_rdma"}
    if any("wifi" in x for x in labels) and not any("ethernet" in x or "socket" in x for x in labels):
        return {"tier": "unsupported", "label": "wifi"}
    if any("ethernet" in x or "socket" in x for x in labels):
        return {"tier": "lan", "label": "ethernet_or_socket"}
    return {"tier": "unknown", "label": "unknown"}


def _model_list_contains(models: list[str], requested: str) -> bool:
    req = requested.strip().lower()
    if not req:
        return False
    return any(str(m).strip().lower() == req for m in models)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _recommended_next(blockers: list[str], warnings: list[str], *, measured_ok: bool | None) -> str:
    if blockers:
        if "wifi_interconnect_unsupported" in blockers:
            return "move exo nodes to Ethernet or Thunderbolt before eval"
        if "canary_failed" in blockers:
            return "fix exo generation before running ts-eval"
        return "fix exo cluster health before eval"
    if measured_ok is None:
        return "run decompute ts-health --measure"
    if warnings:
        return "warnings present; run ts-eval before any auto-serve gate"
    return "run the §0 ts-eval comparison"


def _summarize_topology(topology: dict[str, Any]) -> dict[str, Any]:
    connections = _mapping(topology, "connections")
    edge_count = 0
    connection_types: set[str] = set()
    for sinks in connections.values():
        if not isinstance(sinks, dict):
            continue
        for edges in sinks.values():
            edge_list = edges if isinstance(edges, list) else []
            edge_count += len(edge_list)
            for edge in edge_list:
                edge_d = _as_dict(edge)
                typ = _pick(edge_d, "type", "kind", "tag", "connectionType", "connection_type")
                if typ:
                    connection_types.add(str(typ))
    return {
        "node_count": len(_list(topology, "nodes")),
        "edge_count": edge_count,
        "connection_types": sorted(connection_types),
    }
