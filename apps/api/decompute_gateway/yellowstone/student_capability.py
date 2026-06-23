"""Device-feasibility gate for the local student (plan §"Yellowstone And Routing"; boss requirement).

The hardware reality matters: on a 16 GB Mac the doc's 12B model has no usable quant — Q4_K_M
(7.4 GB) thrashes swap and freezes the machine, and Q2_K loads but produces garbage. So a RAM check
before loading and model/quant tiering are NOT optional: a device that cannot run the configured
student must route to Claude (or use a smaller model), never load the 12B.

This module answers "can THIS device run the configured student model right now?" deterministically,
without loading anything. It is consulted by the policy/eligibility path; it never starts a model.
"""
from __future__ import annotations

from dataclasses import dataclass

from .profiles import DeviceProfile, ModelProfile

# Headroom multiplier over the raw weight: weights + KV cache + runtime overhead. A model needs
# meaningfully more RAM than its file size to run without swap thrash.
_RAM_HEADROOM_FACTOR = 1.6
# Always leave this much RAM for the OS and the user's other apps. Set from the boss's measured
# reality: a 16 GB Mac running macOS + an editor + Claude Code FREEZES on a 7.4 GB (12B Q4_K_M)
# student via swap thrash. With this reserve, 16 GB -> 10 GB usable, which correctly rejects that
# model (needs ~11.8 GB) while a 32 GB machine (26 GB usable) accepts it.
_OS_RESERVED_GB = 6.0


@dataclass(frozen=True)
class CapabilityVerdict:
    feasible: bool
    reason: str
    required_gb: float
    available_gb: float
    model_id: str


def required_ram_gb(model: ModelProfile) -> float:
    """Estimate RAM needed to run a local model, from its weight size + headroom. Returns 0 for a
    non-local (remote) model."""
    if model.provider != "local" or not model.local_weight_gb:
        return 0.0
    return round(model.local_weight_gb * _RAM_HEADROOM_FACTOR, 2)


def assess_local_student(*, device: DeviceProfile | None, model: ModelProfile) -> CapabilityVerdict:
    """Decide whether `device` can run local `model`. Conservative: an unknown device is infeasible."""
    if model.provider != "local":
        return CapabilityVerdict(False, "model_not_local", 0.0, 0.0, model.id)
    if device is None:
        return CapabilityVerdict(False, "no_local_device", required_ram_gb(model), 0.0, model.id)
    if device.kind == "remote_provider":
        return CapabilityVerdict(False, "device_is_remote", required_ram_gb(model), device.memory_gb, model.id)

    need = required_ram_gb(model)
    usable = max(0.0, device.memory_gb - _OS_RESERVED_GB)
    if need <= 0:
        return CapabilityVerdict(False, "unknown_model_size", 0.0, usable, model.id)
    if usable < need:
        # The 16 GB Mac case: usable ~12 GB, a 12B Q4 needs ~12 GB after headroom -> too tight.
        return CapabilityVerdict(False, "insufficient_ram", need, round(usable, 2), model.id)
    return CapabilityVerdict(True, "ok", need, round(usable, 2), model.id)
