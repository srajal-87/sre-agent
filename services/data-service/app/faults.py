"""In-memory fault state and the /admin/fault control surface.

Fault state lives in process memory (no restart needed to inject/clear). Only
the fault type(s) this service supports are accepted; unknown types are 400s.
Ground truth is recorded by the injector, never here.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# The data-service only knows how to fault its configuration: `bad_config`.
SUPPORTED_FAULTS = {"bad_config"}


class FaultState:
    """Holds the single active fault (if any) for this service."""

    def __init__(self) -> None:
        self.active_fault: str | None = None
        self.params: dict = {}

    def enable(self, fault: str, params: dict | None = None) -> None:
        self.active_fault = fault
        self.params = params or {}

    def disable(self) -> None:
        self.active_fault = None
        self.params = {}

    # `clear` is an alias used by tests/injector for an unconditional reset.
    clear = disable

    def is_active(self, fault: str) -> bool:
        return self.active_fault == fault


fault_state = FaultState()


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
        fault_state.disable()
    return {"fault": req.fault, "enabled": req.enabled, "params": fault_state.params}


@router.delete("/admin/fault")
def clear_fault():
    fault_state.clear()
    return {"cleared": True}
