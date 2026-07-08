from __future__ import annotations

import asyncio

import httpx

from decompute_gateway import exo_adapter as exo


def test_exo_root_url_strips_v1_suffix():
    assert exo.exo_root_url("http://127.0.0.1:52415/v1") == "http://127.0.0.1:52415"
    assert exo.exo_root_url("http://host:52415/api/v1/") == "http://host:52415/api"


def test_extract_model_ids_accepts_exo_and_openai_shapes():
    assert exo.extract_model_ids([{"id": "m1"}, {"modelId": "m2"}]) == ["m1", "m2"]
    assert exo.extract_model_ids({"data": [{"id": "m3"}]}) == ["m3"]


def test_summarize_exo_state_normalizes_topology_and_nodes():
    gib = 1024 ** 3
    state = {
        "nodeIdentities": {
            "n1": {"friendlyName": "studio", "modelId": "Mac14,13", "chipId": "M2 Ultra"},
            "n2": {"friendlyName": "mini", "modelId": "Mac16,10", "chipId": "M4 Pro"},
        },
        "nodeMemory": {
            "n1": {"ramTotal": {"inBytes": 128 * gib}, "ramAvailable": {"inBytes": 80 * gib}},
            "n2": {"ramTotal": {"inBytes": 64 * gib}, "ramAvailable": {"inBytes": 40 * gib}},
        },
        "nodeNetwork": {
            "n1": {"interfaces": [{"name": "bridge0", "interfaceType": "thunderbolt"}]},
            "n2": {"interfaces": [{"name": "en0", "interfaceType": "ethernet"}]},
        },
        "topology": {
            "nodes": ["n1", "n2"],
            "connections": {"n1": {"n2": [{"type": "SocketConnection"}]}},
        },
        "instances": {
            "i1": {
                "instanceId": "i1",
                "shardAssignments": {
                    "modelId": "mlx-community/qwen-coder",
                    "runnerToShard": {"r1": {}},
                },
            },
        },
    }
    cap = exo.summarize_exo_state(state)
    assert cap["ok"] is True
    assert cap["node_count"] == 2
    assert cap["pooled_ram_gb"] == 192
    assert cap["available_ram_gb"] == 120
    assert cap["active_models"] == ["mlx-community/qwen-coder"]
    assert cap["network"]["types"] == ["ethernet", "thunderbolt"]
    assert cap["topology"]["edge_count"] == 1


def test_assess_exo_cluster_flags_wifi_and_missing_model_state():
    summary = {
        "ok": True,
        "node_count": 2,
        "network": {"types": ["wifi"]},
        "topology": {"edge_count": 0, "connection_types": []},
        "active_models": [],
    }
    verdict = exo.assess_exo_cluster(
        summary,
        requested_model="mlx-community/qwen-coder",
        models_loaded=["mlx-community/qwen-coder"],
        measured_ok=True,
    )
    assert verdict["ok"] is False
    assert "wifi_interconnect_unsupported" in verdict["blockers"]
    assert "requested_model_not_active" in verdict["warnings"]
    assert verdict["model"] == {
        "requested": "mlx-community/qwen-coder",
        "downloaded": True,
        "active": False,
    }


def test_exo_cluster_capability_fetches_state_and_reports_api_formats():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/state"
        return httpx.Response(200, json={"nodeIdentities": {"n1": {}}, "topology": {"nodes": ["n1"]}})

    cap = asyncio.run(
        exo.exo_cluster_capability(
            base_url="http://127.0.0.1:52415/v1",
            requested_model="mlx-community/qwen-coder",
            models_loaded=["mlx-community/qwen-coder"],
            transport=httpx.MockTransport(handler),
        )
    )
    assert cap["ok"] is True
    assert cap["backend"] == "exo"
    assert cap["node_count"] == 1
    assert cap["api_formats"]["claude_messages"] == "/v1/messages"
    assert cap["readiness"]["model"]["downloaded"] is True
