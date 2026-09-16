"""query_metrics — read time series out of Prometheus.

**The agent does not write PromQL; this module builds it.** ``KNOWN_METRICS``
describes the six metrics the stack actually exposes, and the input names a
metric plus filters and an aggregation. The metric surface here is small and
fully known, so free-form PromQL would buy nothing but hallucinated metric
names, empty results misread as "no problem", and non-reproducible eval runs.
The built string is returned in ``ToolResult.query``, so the agent still sees
and cites real PromQL. (A raw-PromQL escape hatch is a deliberate deferral, to
be added behind an allow-list once the reasoning loop works.)

Note: the Prometheus TSDB in this stack is **ephemeral** — docker-compose mounts
no volume at /prometheus, so all history dies on ``docker compose down``. An
empty result right after a restart means "no data retained", not "no traffic".

The pure builders below **raise** on a bad query; ``query_metrics`` catches that
and returns the ``ok=False`` envelope. Strict core, forgiving boundary.
"""

import math
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx
from pydantic import BaseModel, Field, field_validator

from agent import config
from agent.tools.base import TimeWindow, ToolResult, failure, utc_now

# Every victim service listens on 8000 inside the compose network, and
# infra/prometheus.yml scrapes them as "<service>:8000" — which is exactly the
# `instance` label value. Verified against a live /api/v1/query.
METRIC_PORT = 8000

# The six metrics the stack exposes, verified against live /metrics output.
# prometheus_client strips a trailing _total and re-appends it, so the exposed
# counter name is http_requests_total (not http_requests_total_total).
KNOWN_METRICS = {
    "http_requests_total": {
        "type": "counter",
        "labels": ["method", "path", "status"],
        "services": ["api-gateway", "data-service", "downstream-dep"],
        "help": "Total HTTP requests handled, by method, path and status.",
    },
    "http_request_duration_seconds": {
        "type": "histogram",
        "labels": ["method", "path"],
        "services": ["api-gateway", "data-service", "downstream-dep"],
        "help": "HTTP request latency in seconds, by method and path.",
    },
    "upstream_timeouts_total": {
        "type": "counter",
        "labels": [],
        "services": ["api-gateway"],
        "help": "Upstream calls that exceeded the gateway's timeout budget.",
    },
    "config_errors_total": {
        "type": "counter",
        "labels": [],
        "services": ["data-service"],
        "help": "Requests that failed due to a bad configuration value.",
    },
    "config_version": {
        "type": "gauge",
        "labels": [],
        "services": ["data-service"],
        "help": "Version number of the currently loaded configuration.",
    },
    "downstream_memory_bytes": {
        "type": "gauge",
        "labels": [],
        "services": ["downstream-dep"],
        "help": "Bytes held by the injected memory-pressure fault.",
    },
}

# Which aggregations make sense for which metric type. Asking for p99 of a
# counter is a modelling error, not a query that happens to return nothing.
AGGREGATIONS_BY_TYPE = {
    "counter": ["rate", "sum"],
    "histogram": ["p95", "p99", "avg"],
    "gauge": ["last", "avg"],
}

# The sensible aggregation for each type, used when the caller names none. A
# single fixed default cannot work: "rate" is meaningless for a gauge, so a
# fixed default would fail every config_version and latency query outright.
DEFAULT_AGGREGATION = {"counter": "rate", "histogram": "p99", "gauge": "last"}

LOOKBACK_MINUTES_RANGE = (1, 120)
STEP_SECONDS_RANGE = (5, 300)

# A rate window shorter than the step silently skips samples between points, and
# one shorter than ~4 scrape intervals is too noisy to read. 60s is both.
MIN_RATE_WINDOW_SECONDS = 60


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class MetricsQuery(BaseModel):
    """What to ask Prometheus for.

    Out-of-range lookbacks and steps are **clamped, not rejected** — an LLM will
    send silly numbers, and failing the whole call over one is a wasted turn.
    """

    metric: str = Field(
        description=(
            "One of: http_requests_total (counter), "
            "http_request_duration_seconds (histogram), upstream_timeouts_total "
            "(counter, api-gateway), config_errors_total (counter, data-service), "
            "config_version (gauge, data-service), downstream_memory_bytes "
            "(gauge, downstream-dep)."
        )
    )
    service: str | None = Field(
        default=None,
        description="api-gateway, data-service, or downstream-dep.",
    )
    path: str | None = Field(
        default=None, description="Request path, e.g. /request, /process, /data."
    )
    status: str | None = Field(default=None, description="HTTP status, e.g. '504'.")
    # None means "whatever suits this metric's type" - see DEFAULT_AGGREGATION.
    aggregation: str | None = Field(
        default=None,
        description=(
            "rate|sum for counters, p95|p99|avg for histograms, last|avg for "
            "gauges. Omit to get the sensible default for the metric's type."
        ),
    )
    lookback_minutes: int = Field(default=15, description="1 to 120; clamped.")
    step_seconds: int = Field(default=30, description="5 to 300; clamped.")
    since: datetime | None = Field(
        default=None, description="Absolute window start; overrides lookback_minutes."
    )
    until: datetime | None = Field(default=None, description="Absolute window end.")

    @field_validator("lookback_minutes")
    @classmethod
    def _clamp_lookback(cls, value: int) -> int:
        return _clamp(value, *LOOKBACK_MINUTES_RANGE)

    @field_validator("step_seconds")
    @classmethod
    def _clamp_step(cls, value: int) -> int:
        return _clamp(value, *STEP_SECONDS_RANGE)


def resolve_window(
    q: MetricsQuery, *, now: Callable[[], datetime] = utc_now
) -> TimeWindow:
    """Turn the query's time fields into a concrete window.

    Absolute ``since``/``until`` win over ``lookback_minutes``; ``since`` alone
    runs up to now.
    """
    if q.since is None:
        return TimeWindow.lookback(q.lookback_minutes, now=now)
    return TimeWindow(start=q.since, end=q.until or now())


def _escape(value: str) -> str:
    """Escape a PromQL label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _validate(q: MetricsQuery) -> tuple[dict, str]:
    """Return (catalogue entry, resolved aggregation), or raise ValueError."""
    spec = KNOWN_METRICS.get(q.metric)
    if spec is None:
        raise ValueError(
            f"unknown metric '{q.metric}'; known: {sorted(KNOWN_METRICS)}"
        )

    aggregation = q.aggregation or DEFAULT_AGGREGATION[spec["type"]]
    valid_aggregations = AGGREGATIONS_BY_TYPE[spec["type"]]
    if aggregation not in valid_aggregations:
        raise ValueError(
            f"aggregation '{aggregation}' is not valid for {spec['type']} "
            f"metric '{q.metric}'; valid: {valid_aggregations}"
        )

    if q.service is not None and q.service not in spec["services"]:
        raise ValueError(
            f"metric '{q.metric}' is not exposed by '{q.service}'; "
            f"exposed by: {spec['services']}"
        )

    # Filtering on a label the metric doesn't carry matches nothing, and an
    # empty result reads as "no problem" — so reject it instead.
    for label in ("path", "status"):
        if getattr(q, label) is not None and label not in spec["labels"]:
            raise ValueError(
                f"metric '{q.metric}' has no label '{label}'; "
                f"labels: {spec['labels'] or 'none'}"
            )

    return spec, aggregation


def _selector(q: MetricsQuery) -> str:
    """Build the ``{label="value",...}`` matcher, or "" when unfiltered."""
    matchers = []
    if q.service:
        matchers.append(f'instance="{_escape(q.service)}:{METRIC_PORT}"')
    if q.path:
        matchers.append(f'path="{_escape(q.path)}"')
    if q.status:
        matchers.append(f'status="{_escape(q.status)}"')
    return "{" + ",".join(matchers) + "}" if matchers else ""


def _grouping(spec: dict) -> list[str]:
    """Labels to group by: the instance, plus every label the metric declares.

    Keeping ``method`` in the grouping means GET and POST latencies are never
    silently averaged into one incomparable number.
    """
    return ["instance"] + list(spec["labels"])


def _rate_window(q: MetricsQuery) -> str:
    seconds = max(MIN_RATE_WINDOW_SECONDS, q.step_seconds)
    return f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"


def build_query(q: MetricsQuery) -> str:
    """Build the PromQL for ``q``. Raises ValueError on an invalid query."""
    spec, aggregation = _validate(q)
    selector = _selector(q)
    group_by = ", ".join(_grouping(spec))
    window = _rate_window(q)
    name = q.metric

    if spec["type"] == "counter":
        if aggregation == "rate":
            return f"sum by ({group_by}) (rate({name}{selector}[{window}]))"
        return f"sum by ({group_by}) ({name}{selector})"

    if spec["type"] == "histogram":
        if aggregation == "avg":
            numerator = f"sum by ({group_by}) (rate({name}_sum{selector}[{window}]))"
            denominator = f"sum by ({group_by}) (rate({name}_count{selector}[{window}]))"
            return f"{numerator} / {denominator}"
        quantile = "0.95" if aggregation == "p95" else "0.99"
        buckets = (
            f"sum by (le, {group_by}) (rate({name}_bucket{selector}[{window}]))"
        )
        return f"histogram_quantile({quantile}, {buckets})"

    # gauge
    if aggregation == "avg":
        return f"avg by ({group_by}) ({name}{selector})"
    return f"{name}{selector}"


class MetricPoint(BaseModel):
    ts: datetime
    value: float


class MetricSeries(BaseModel):
    labels: dict[str, str]
    points: list[MetricPoint] = Field(default_factory=list)
    min: float = 0.0
    max: float = 0.0
    mean: float = 0.0
    latest: float = 0.0


class MetricsResult(ToolResult):
    series: list[MetricSeries] = Field(default_factory=list)
    series_count: int = 0


# Ceilings on what goes back to the LLM. Beyond these the payload costs more in
# tokens than it carries in signal.
MAX_SERIES = 20
MAX_POINTS = 120


def _downsample(points: list[MetricPoint]) -> list[MetricPoint]:
    """Thin ``points`` to at most MAX_POINTS, always keeping first and last.

    The latest point is the one the agent reasons about, so it must survive.
    """
    if len(points) <= MAX_POINTS:
        return points
    last = len(points) - 1
    indexes = sorted(
        {round(i * last / (MAX_POINTS - 1)) for i in range(MAX_POINTS)}
    )
    return [points[i] for i in indexes]


def parse_range_response(
    payload: dict,
) -> tuple[list[MetricSeries], bool, list[str]]:
    """Turn a query_range payload into series, a truncation flag, and notes.

    Prometheus renders a missing value as the JSON **string** ``"NaN"`` (and
    ``"+Inf"``/``"-Inf"``) — most often from ``histogram_quantile`` over an empty
    bucket. Those points are dropped rather than carried as ``float('nan')``,
    which would break a strict ``json.dumps`` and poison the ``evidence`` jsonb
    write. They are excluded from min/max/mean too, so a mostly-idle histogram
    doesn't report a nonsense average.
    """
    results = payload.get("data", {}).get("result", [])
    notes: list[str] = []
    truncated = False

    if len(results) > MAX_SERIES:
        notes.append(
            f"{len(results)} series matched; returning the first {MAX_SERIES}."
        )
        truncated = True
        results = results[:MAX_SERIES]

    series: list[MetricSeries] = []
    dropped_points = 0
    dropped_series = 0

    for result in results:
        labels = {k: v for k, v in result.get("metric", {}).items() if k != "__name__"}
        points: list[MetricPoint] = []
        for ts, raw_value in result.get("values", []):
            value = float(raw_value)
            if not math.isfinite(value):
                dropped_points += 1
                continue
            points.append(
                MetricPoint(ts=datetime.fromtimestamp(float(ts), tz=timezone.utc), value=value)
            )

        if not points:
            dropped_series += 1
            continue

        # Stats come from the *full* point list: downsampling a two-scrape
        # latency spike away would report a healthy max during a live fault.
        values = [p.value for p in points]
        kept = _downsample(points)
        if len(kept) < len(points):
            truncated = True

        series.append(
            MetricSeries(
                labels=labels,
                points=kept,
                min=min(values),
                max=max(values),
                mean=sum(values) / len(values),
                latest=values[-1],
            )
        )

    if dropped_points or dropped_series:
        note = f"dropped {dropped_points} NaN/Inf point(s)"
        if dropped_series:
            note += f" and {dropped_series} series that were entirely NaN/Inf"
        # ASCII only: these strings get printed to a Windows console by probe.py.
        notes.append(note + " - typically an empty histogram bucket.")

    return series, truncated, notes


def _fmt(value: float) -> str:
    return f"{value:.4g}"


def _is_flat(series: MetricSeries) -> bool:
    """True when the spread is negligible relative to the magnitude.

    Exact float equality is not enough: a genuinely constant histogram quantile
    comes back as 0.00495 and 0.0049499999999999995, which would otherwise be
    described as having moved.
    """
    span = series.max - series.min
    scale = max(abs(series.max), abs(series.min))
    return span <= scale * 1e-9


def _stale_by_seconds(series: MetricSeries, window_end: datetime) -> float:
    """How far the newest surviving point lags the end of the window."""
    return (window_end - series.points[-1].ts).total_seconds()


def _is_stale(series: MetricSeries, window_end: datetime) -> bool:
    """True when the series stopped reporting before the window ended.

    MIN_RATE_WINDOW_SECONDS is the principled threshold rather than a new magic
    number: it is the rate window, so a gap wider than it means any rate over
    this series already reads zero. Below it the series is merely a scrape or
    two behind, which is normal.
    """
    return _stale_by_seconds(series, window_end) > MIN_RATE_WINDOW_SECONDS


def _describe(series: MetricSeries, window_end: datetime) -> str:
    """One clause describing where a series went.

    A series whose points stop well before the window ends did not "stay flat" -
    it stopped reporting, most often because histogram_quantile over an empty
    bucket returned NaN and those points were dropped. Describing a trend from
    the surviving, pre-fault points presents a stale value as a current one,
    which reads as reassurance exactly when it is the opposite.
    """
    if _is_stale(series, window_end):
        last = series.points[-1].ts
        gap = _stale_by_seconds(series, window_end) / 60.0
        return (
            f"stopped reporting - last sample {last.strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"(value {_fmt(series.latest)}), no samples in the {gap:.1f}m since"
        )

    first = series.points[0].value
    last_value = series.latest
    if _is_flat(series):
        return f"flat at {_fmt(last_value)}"
    direction = "rose" if last_value > first else "fell" if last_value < first else "moved"
    return (
        f"{direction} from {_fmt(first)} to {_fmt(last_value)} "
        f"(min {_fmt(series.min)}, max {_fmt(series.max)})"
    )


# What identifies a series in a one-line summary. ``method`` is in here because
# it is part of the series identity (see _grouping): without it two genuinely
# different series render as the same string, which is invisible while only one
# series is described and confusing once three are.
SUMMARY_LABELS = ("instance", "method", "path", "status")


def _label_bits(series: MetricSeries) -> str:
    """The labels worth naming in a one-line summary."""
    return " ".join(
        series.labels[k] for k in SUMMARY_LABELS if k in series.labels
    )


def _change(series: MetricSeries) -> float:
    """How far the series travelled, at its widest.

    Not the gap between the endpoints: a rate() series rises and falls back
    around any bounded incident, so the endpoints are both near zero and the
    series carrying the fault would score lower than idle background traffic.
    min/max are computed before downsampling, so the peak survives thinning.
    """
    return series.max - series.min


# A series present for only a sliver of the window was never reporting
# continuously, so its silence is its normal state rather than an event.
# Measured against the live stack: a one-shot administrative endpoint, which
# receives a request only when a fault is set or cleared, covers 3-7% of the
# window and is stale by construction, while a service actually carrying
# traffic covers 25-35%. The line goes between them.
MIN_COVERAGE_FOR_STALENESS = 0.15


def _coverage(series: MetricSeries, window: TimeWindow, step_seconds: int) -> float:
    """What fraction of the window's expected points this series actually has.

    Expected is capped at MAX_POINTS because a longer series is downsampled to
    that ceiling - without the cap a densely-reporting series over a long window
    would look sparse purely because it was thinned.
    """
    expected = min(MAX_POINTS, max(1.0, window.duration_minutes * 60.0 / step_seconds))
    return len(series.points) / expected


def _notability(
    series: MetricSeries, window: TimeWindow, step_seconds: int
) -> tuple:
    """Sort key: most notable first, and deterministic.

    A series that stopped reporting outranks one that merely moved - going from
    reporting to not reporting is the larger event, and its surviving points
    barely move, so ranking on change alone would bury it.

    But only if it was reporting in the first place. A series that produced a
    handful of samples across the window never established a signal to lose,
    and because it is permanently stale it would otherwise hold the top slots
    on every query forever. Ties break on the label string so two identical
    investigations read identical evidence regardless of the order Prometheus
    happened to return.
    """
    established = _coverage(series, window, step_seconds) >= MIN_COVERAGE_FOR_STALENESS
    return (
        0 if (_is_stale(series, window.end) and established) else 1,
        -_change(series),
        _label_bits(series),
    )


# How many series one summary line describes. Bounded because this line is all
# the model still sees for every iteration but the newest (graph/messages.py).
MAX_DESCRIBED_SERIES = 3


def summarise(
    q: MetricsQuery, series: list[MetricSeries], *, window: TimeWindow
) -> str:
    """One line, written for the LLM to reason on.

    ``window`` is required rather than defaulted: a series is stale only
    relative to the window that was asked for, and present-or-absent only
    relative to its span. A default would silently describe a dead series as a
    healthy one at every call site that forgot it.
    """
    spec = KNOWN_METRICS.get(q.metric, {})
    aggregation = q.aggregation or DEFAULT_AGGREGATION.get(spec.get("type", ""), "")
    filters = [f"{k}={v}" for k, v in
               (("service", q.service), ("path", q.path), ("status", q.status)) if v]
    scope = " ".join(filters) if filters else "all services"

    if not series:
        return (
            f"No data: {aggregation} {q.metric} for {scope} matched no series in "
            f"the last {q.lookback_minutes}m. Either no traffic matched those "
            f"filters, or Prometheus retained no samples for that window."
        )

    # The window goes before the description, not after: a stale series ends its
    # clause on an absolute time, and "no samples in the 7.0m since over 30m"
    # does not parse.
    if len(series) == 1:
        lead = series[0]
        return (
            f"{aggregation} {q.metric} for {_label_bits(lead) or scope} over "
            f"{q.lookback_minutes}m: {_describe(lead, window.end)}."
        )

    ranked = sorted(
        series, key=lambda s: _notability(s, window, q.step_seconds)
    )
    described = "; ".join(
        f"{_label_bits(s) or scope} {_describe(s, window.end)}"
        for s in ranked[:MAX_DESCRIBED_SERIES]
    )
    point_count = sum(len(s.points) for s in series)
    return (
        f"{aggregation} {q.metric} for {scope} over {q.lookback_minutes}m: "
        f"{described}. {len(series)} series, {point_count} points."
    )


# ── the tool itself ──────────────────────────────────────────────────

TOOL_NAME = "query_metrics"
HTTP_TIMEOUT_SECONDS = 10.0
RANGE_PATH = "/api/v1/query_range"


async def _http_fetch(url: str, params: dict) -> dict:
    """GET ``url`` and return the parsed JSON body.

    Deliberately does **not** raise for a 4xx: Prometheus reports a bad query as
    a 400 whose JSON body carries the real explanation, and that explanation is
    far more useful to the agent than "HTTP 400". A per-call client in an
    ``async with`` matches services/api-gateway/app/main.py.
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(url, params=params)
        return response.json()


async def query_metrics(
    q: MetricsQuery,
    *,
    fetch: Callable[[str, dict], Awaitable[dict]] = _http_fetch,
    now: Callable[[], datetime] = utc_now,
    base_url: str | None = None,
) -> MetricsResult:
    """Read a time series out of Prometheus.

    Never raises for an expected failure — an unreachable server, a bad metric
    name, or a Prometheus error all come back as ``ok=False`` with a readable
    ``error``, so the agent can adapt instead of the graph dying.

    Note: this stack's Prometheus has **no volume** for /prometheus, so its TSDB
    is wiped by ``docker compose down``. Empty results after a restart mean "no
    retained samples", not "no traffic".
    """
    started = time.perf_counter()
    prometheus_url = (base_url or config.PROMETHEUS_URL).rstrip("/")
    url = prometheus_url + RANGE_PATH
    source = f"prometheus @ {url}"

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    # Strict core, forgiving boundary: the builders raise, the tool reports.
    try:
        promql = build_query(q)
    except ValueError as exc:
        return failure(
            tool=TOOL_NAME,
            source=source,
            query="",
            error=str(exc),
            summary=f"Could not build a query: {exc}",
            model=MetricsResult,
            latency_ms=_elapsed_ms(),
        )

    window = resolve_window(q, now=now)
    params = {
        "query": promql,
        "start": window.unix_start,
        "end": window.unix_end,
        "step": f"{q.step_seconds}s",
    }

    try:
        payload = await fetch(url, params)
    except httpx.TimeoutException:
        return failure(
            tool=TOOL_NAME, source=source, query=promql, window=window,
            error=f"prometheus timed out after {HTTP_TIMEOUT_SECONDS:.0f}s at {url}",
            summary=(
                "Prometheus did not respond in time; try a shorter "
                "lookback_minutes, or use query_logs instead."
            ),
            model=MetricsResult, latency_ms=_elapsed_ms(),
        )
    except (httpx.HTTPError, ValueError) as exc:
        return failure(
            tool=TOOL_NAME, source=source, query=promql, window=window,
            error=f"prometheus unreachable at {prometheus_url}: {exc}",
            summary=(
                "Could not reach Prometheus, so no metric evidence is available; "
                "use query_logs instead."
            ),
            model=MetricsResult, latency_ms=_elapsed_ms(),
        )

    if payload.get("status") != "success":
        # Prometheus's own wording beats anything we could paraphrase.
        detail = payload.get("error", "prometheus returned a non-success status")
        return failure(
            tool=TOOL_NAME, source=source, query=promql, window=window,
            error=detail,
            summary=f"Prometheus rejected the query: {detail}",
            model=MetricsResult, latency_ms=_elapsed_ms(),
        )

    series, truncated, notes = parse_range_response(payload)
    return MetricsResult(
        tool=TOOL_NAME,
        summary=summarise(q, series, window=window),
        source=source,
        query=promql,
        window=window,
        truncated=truncated,
        notes=notes,
        latency_ms=_elapsed_ms(),
        series=series,
        series_count=len(series),
    )
