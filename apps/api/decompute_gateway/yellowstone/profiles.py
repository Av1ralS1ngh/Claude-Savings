from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
import platform
import subprocess
from typing import Any


@dataclass
class DeviceProfile:
    id: str
    name: str
    kind: str  # cpu|apple_silicon|gpu|remote_provider
    memory_gb: float
    compute_score: float
    network_score: float = 1.0
    runtimes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ModelProfile:
    id: str
    provider: str = "anthropic"
    context_window_tokens: int = 200_000
    supports_prompt_cache: bool = True
    supports_tool_use: bool = True
    local_weight_gb: float | None = None
    layers: int | None = None
    hidden_size: int | None = None
    num_kv_heads: int | None = None
    head_dim: int | None = None


@dataclass
class TrafficProfile:
    raw_input_tokens: int
    compressed_input_tokens: int
    p95_output_tokens: int = 1024
    max_concurrent_requests: int = 8
    target_ttft_ms: int = 2000
    workload_class: str = "interactive"


@dataclass
class RuntimeProfile:
    name: str
    kind: str  # remote_claude|local_llama_cpp|local_vllm|mock
    device_id: str
    max_context_tokens: int
    supports_streaming: bool = True
    supports_tools: bool = True
    supports_prompt_cache: bool = False


@dataclass
class InferencePlan:
    id: str
    model: str
    route: str
    selected_runtime: RuntimeProfile
    compression_mode: str
    prompt_cache_mode: str
    raw_input_tokens: int
    compressed_input_tokens: int
    tokens_saved: int
    compression_ratio: float
    estimated_kv_cache_mb: float | None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _mac_memory_gb() -> float:
    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, stderr=subprocess.DEVNULL).strip()
        return round(int(out) / 1024**3, 2)
    except Exception:
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return round(pages * page_size / 1024**3, 2)
        except Exception:
            return 8.0


def scan_local_devices() -> list[DeviceProfile]:
    machine = platform.machine().lower()
    system = platform.system().lower()
    cpu_count = os.cpu_count() or 1
    memory_gb = _mac_memory_gb()

    devices = [
        DeviceProfile(
            id="dev_local_cpu",
            name=f"{platform.node() or 'local'} CPU",
            kind="cpu",
            memory_gb=memory_gb,
            compute_score=max(1.0, cpu_count / 4),
            runtimes=["mock", "yscompress", "anthropic_proxy"],
            tags=[system, machine],
        )
    ]

    if system == "darwin" and machine in {"arm64", "aarch64"}:
        devices.append(
            DeviceProfile(
                id="dev_apple_silicon",
                name="Apple Silicon unified memory",
                kind="apple_silicon",
                memory_gb=memory_gb,
                compute_score=max(2.0, cpu_count / 3),
                runtimes=["mlx", "llama_cpp", "mock", "yscompress", "anthropic_proxy"],
                tags=["mac", "metal", "unified_memory"],
            )
        )

    # Development convenience: let the planner exercise GPU routing on a Mac.
    if os.getenv("YELLOWSTONE_FAKE_GPU", "false").lower() == "true":
        devices.append(
            DeviceProfile(
                id="dev_fake_gpu",
                name="Fake local GPU for planner testing",
                kind="gpu",
                memory_gb=float(os.getenv("YELLOWSTONE_FAKE_GPU_VRAM_GB", "24")),
                compute_score=8.0,
                runtimes=["vllm", "sglang", "mock"],
                tags=["fake", "dev"],
            )
        )

    # Remote Claude is modeled as a runtime target, not as local weights.
    devices.append(
        DeviceProfile(
            id="dev_anthropic_remote",
            name="Anthropic Claude API",
            kind="remote_provider",
            memory_gb=10_000,
            compute_score=100.0,
            network_score=0.8,
            runtimes=["anthropic_proxy"],
            tags=["remote", "claude"],
        )
    )
    return devices
