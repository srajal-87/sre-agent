import asyncio
import os
import time
import uuid

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.logging import current_trace_id, get_logger
from app import metrics
from app.faults import fault_state, router as fault_router

SERVICE_NAME = "api-gateway"

app = FastAPI(title=SERVICE_NAME)
log = get_logger(SERVICE_NAME)

TRACE_HEADER = "X-Trace-Id"
DATA_SERVICE_URL = os.getenv("DATA_SERVICE_URL", "http://data-service:8000")
UPSTREAM_TIMEOUT_SECONDS = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "2.0"))


async def call_upstream(trace_id: str | None) -> dict:
    """Call data-service /process, propagating the trace_id header."""
    headers = {TRACE_HEADER: trace_id} if trace_id else {}
    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            f"{DATA_SERVICE_URL}/process", headers=headers
        )
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


@app.get("/request")
async def request_endpoint():
    trace_id = current_trace_id.get()
    try:
        if fault_state.is_active("timeout"):
            # Injected fault: the gateway waits out its budget, then gives up.
            await asyncio.sleep(UPSTREAM_TIMEOUT_SECONDS)
            raise httpx.TimeoutException("injected upstream timeout")
        upstream = await call_upstream(trace_id)
    except httpx.TimeoutException:
        metrics.upstream_timeouts_total.inc()
        log.error("upstream timeout calling data-service")
        return JSONResponse(status_code=504, content={"error": "gateway timeout"})
    return {"service": SERVICE_NAME, "upstream": upstream}


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}
