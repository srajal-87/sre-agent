"""In-memory fault state and the /admin/fault control surface.

Unlike the other two services, downstream-dep supports two independent faults
(`latency` and `memory`), so fault state is a map rather than a single slot.
Fault state lives in process memory; ground truth is recorded by the injector.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import metrics

SUPPORTED_FAULTS = {"latency", "memory"}

# Default block size for the memory-pressure fault when `bytes` is not given.
DEFAULT_MEMORY_BYTES = 50 * 1024 * 1024  # 50 MB

# Held here so the injected memory block persists across requests.
_memory_block: bytearray | None = None


class FaultState:
    """Holds every currently-active fault and its params."""

    def __init__(self) -> None:
        self.active: dict[str, dict] = {}

    def enable(self, fault: str, params: dict | None = None) -> None:
        self.active[fault] = params or {}

    def disable(self, fault: str) -> None:
        self.active.pop(fault, None)

    def clear(self) -> None:
        self.active.clear()

    def is_active(self, fault: str) -> bool:
        return fault in self.active

    def params_for(self, fault: str) -> dict:
        return self.active.get(fault, {})


fault_state = FaultState()


def _apply_memory(enabled: bool, params: dict) -> None:
    """Allocate/free the held memory block and reflect it in the gauge."""
    global _memory_block
    if enabled:
        n = int(params.get("bytes", DEFAULT_MEMORY_BYTES))
        _memory_block = bytearray(n)
        metrics.downstream_memory_bytes.set(n)
    else:
        _memory_block = None
        metrics.downstream_memory_bytes.set(0)


class FaultRequest(BaseModel):
    fault: str
    enabled: bool
    params: dict = Field(default_factory=dict)


router = APIRouter()


@router.post("/admin/fault")
def set_fault(req: FaultRequest):
    if req.fault not in SUPPORTED_FAULTS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported fault '{req.fault}'; supported: {sorted(SUPPORTED_FAULTS)}",
        )
    if req.enabled:
        fault_state.enable(req.fault, req.params)
    else:
        fault_state.disable(req.fault)
    if req.fault == "memory":
        _apply_memory(req.enabled, req.params)
    return {"fault": req.fault, "enabled": req.enabled, "params": fault_state.params_for(req.fault)}


@router.delete("/admin/fault")
def clear_fault():
    fault_state.clear()
    _apply_memory(False, {})  # release any held memory too
    return {"cleared": True}
