import asyncio
import time
import uuid

from fastapi import FastAPI, Request, Response

from app.logging import current_trace_id, get_logger
from app import metrics
from app.faults import fault_state, router as fault_router

SERVICE_NAME = "downstream-dep"

app = FastAPI(title=SERVICE_NAME)
log = get_logger(SERVICE_NAME)

TRACE_HEADER = "X-Trace-Id"
DEFAULT_LATENCY_MS = 3000

# Initialise the memory gauge so it always exports a value.
metrics.downstream_memory_bytes.set(0)


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
        # Don't record metrics for scrapes of the metrics endpoint itself.
        if path != "/metrics":
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


@app.get("/data")
async def data():
    if fault_state.is_active("latency"):
        delay_ms = int(fault_state.params_for("latency").get("delay_ms", DEFAULT_LATENCY_MS))
        log.warning("injected latency", extra={"delay_ms": delay_ms})
        await asyncio.sleep(delay_ms / 1000)
    return {"service": SERVICE_NAME, "data": "ok", "value": 42}


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}
