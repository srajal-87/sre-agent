"""Prometheus metrics for api-gateway.

Uses the default registry; each container runs a single uvicorn worker, so no
multiprocess mode is required.
"""

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled, labelled by method, path and status.",
    ["method", "path", "status"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds, labelled by method and path.",
    ["method", "path"],
)

upstream_timeouts_total = Counter(
    "upstream_timeouts_total",
    "Total upstream calls that exceeded the gateway's timeout budget.",
)


def render() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
