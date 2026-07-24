"""Prometheus metrics for data-service.

Uses the default registry; each container runs a single uvicorn worker, so no
multiprocess mode is required.
"""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

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

config_errors_total = Counter(
    "config_errors_total",
    "Total requests that failed due to a bad configuration value.",
)

config_version = Gauge(
    "config_version",
    "Version number of the currently loaded configuration.",
)


def render() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
