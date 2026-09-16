import os
import time
import uuid

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.logging import current_trace_id, get_logger
from app import metrics
from app.faults import fault_state, router as fault_router

SERVICE_NAME = "data-service"

app = FastAPI(title=SERVICE_NAME)
log = get_logger(SERVICE_NAME)

TRACE_HEADER = "X-Trace-Id"
DOWNSTREAM_URL = os.getenv("DOWNSTREAM_URL", "http://downstream-dep:8000")
DOWNSTREAM_TIMEOUT_SECONDS = float(os.getenv("DOWNSTREAM_TIMEOUT_SECONDS", "5.0"))

# Initial config version so the gauge always exports a value.
CONFIG_VERSION = 1
metrics.config_version.set(CONFIG_VERSION)


async def call_downstream(trace_id: str | None) -> dict:
    """Call downstream-dep /data, propagating the trace_id header."""
    headers = {TRACE_HEADER: trace_id} if trace_id else {}
    async with httpx.AsyncClient(timeout=DOWNSTREAM_TIMEOUT_SECONDS) as client:
        resp = await client.get(f"{DOWNSTREAM_URL}/data", headers=headers)
        resp.raise_for_status()
        return resp.json()


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    """Adopt or generate a trace_id, expose it in context, echo it back."""
    trace_id = request.headers.get(TRACE_HEADER) or str(uuid.uuid4())
    token = current_trace_id.set(trace_id)
    path = request.url.path
    start = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers[TRACE_HEADER] = trace_id
        # Don't record scrapes of the metrics endpoint itself, nor admin traffic:
        # a fault-injection call lands only on the target service at exactly the
        # fault-start instant, which would be an answer key in the agent's evidence.
        if path != "/metrics" and not path.startswith("/admin/"):
            elapsed = time.perf_counter() - start
            metrics.http_request_duration_seconds.labels(
                method=request.method, path=path
            ).observe(elapsed)
            metrics.http_requests_total.labels(
                method=request.method, path=path, status=response.status_code
            ).inc()
            log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": path,
                    "status": response.status_code,
                },
            )
        return response
    finally:
        current_trace_id.reset(token)


@app.get("/metrics")
def prometheus_metrics():
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


app.include_router(fault_router)


@app.get("/process")
async def process():
    trace_id = current_trace_id.get()
    if fault_state.is_active("bad_config"):
        # Corrupted config: the downstream target is invalid, so the call fails.
        metrics.config_errors_total.inc()
        log.error(
            "configuration error: invalid downstream target",
            extra={"config_version": CONFIG_VERSION},
        )
        return JSONResponse(status_code=500, content={"error": "configuration error"})
    downstream = await call_downstream(trace_id)
    return {"service": SERVICE_NAME, "downstream": downstream}


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}
