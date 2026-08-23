"""query_logs — read structured logs out of the victim containers.

There is no log backend in this stack: every service writes single-line JSON to
stdout and nothing collects it, so the container's own stdout *is* the log
store. This module owns the pure half — turning captured text into typed lines,
filtering it, and counting it. Fetching the text over the Docker API lives
alongside it in ``fetch_container_logs``.

**Aggregate first, sample second.** Raw logs drown an LLM and blow the token
budget, so ``level_counts`` and ``message_counts`` are computed over the *full*
match set while ``lines`` returns only the most recent ``limit``. This domain
makes that unusually effective: the services log fixed message strings
("injected latency", "upstream timeout calling data-service"), so a top-N
message histogram is tiny and nearly diagnostic on its own.
"""

import asyncio
import json
import time
from collections import Counter
from datetime import datetime
from typing import Callable

from pydantic import BaseModel, Field, field_validator

from agent import config
from agent.tools.base import TimeWindow, ToolResult, failure, utc_now

TOOL_NAME = "query_logs"

LOOKBACK_MINUTES_RANGE = (1, 60)
LIMIT_RANGE = (1, 200)

MAX_MESSAGE_COUNTS = 10
MAX_UNPARSED_SAMPLES = 3

# container.logs() buffers its whole output in memory, so this is a hard ceiling
# on how much any one container can hand back.
MAX_TAIL = 2000

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"

# Fields the JSON formatter always emits; anything else on a line is an extra
# (delay_ms, config_version, method/path/status).
_KNOWN_FIELDS = {"timestamp", "service", "level", "message", "trace_id"}


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class LogsQuery(BaseModel):
    """What to look for. ``service=None`` means every known service."""

    service: str | None = Field(
        default=None,
        description=(
            "api-gateway, data-service, or downstream-dep. Omit for all three."
        ),
    )
    levels: list[str] | None = Field(
        default=None, description='e.g. ["ERROR", "WARNING"]. Omit for all levels.'
    )
    contains: str | None = Field(
        default=None, description="Case-insensitive substring of the log message."
    )
    trace_id: str | None = Field(
        default=None,
        description=(
            "Follow one request across all three services. The most direct way "
            "to link a downstream cause to an upstream symptom."
        ),
    )
    lookback_minutes: int = Field(default=15, description="1 to 60; clamped.")
    since: datetime | None = Field(
        default=None, description="Absolute window start; overrides lookback_minutes."
    )
    until: datetime | None = Field(default=None, description="Absolute window end.")
    limit: int = Field(
        default=50,
        description="Most recent lines to return, 1 to 200. Counts cover everything matched.",
    )

    @field_validator("lookback_minutes")
    @classmethod
    def _clamp_lookback(cls, value: int) -> int:
        return _clamp(value, *LOOKBACK_MINUTES_RANGE)

    @field_validator("limit")
    @classmethod
    def _clamp_limit(cls, value: int) -> int:
        return _clamp(value, *LIMIT_RANGE)


class LogLine(BaseModel):
    timestamp: datetime | None = None
    service: str
    level: str | None = None
    message: str
    trace_id: str | None = None
    extra: dict = Field(default_factory=dict)
    raw: str | None = None  # set when the line was not JSON


class MessageCount(BaseModel):
    service: str
    level: str | None
    message: str
    count: int


class LogsAggregate(BaseModel):
    """The counted findings, independent of the tool envelope."""

    lines: list[LogLine] = Field(default_factory=list)  # most-recent N
    total_matched: int = 0
    services_seen: list[str] = Field(default_factory=list)
    level_counts: dict[str, int] = Field(default_factory=dict)
    message_counts: list[MessageCount] = Field(default_factory=list)
    unparsed_count: int = 0
    unparsed_samples: list[LogLine] = Field(default_factory=list)
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)


class LogsResult(ToolResult, LogsAggregate):
    """The tool envelope plus the counted findings."""


def parse_line(raw: str, service: str) -> LogLine | None:
    """Parse one line of container output. Returns None for a blank line.

    Anything that is not a JSON object — uvicorn access logs, Python tracebacks
    — is kept verbatim in ``raw`` with no level. Multi-line tracebacks stay as
    separate raw lines: grouping them is not worth the complexity, because the
    *count* is the signal.
    """
    text = raw.strip()
    if not text:
        return None

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        payload = None

    if not isinstance(payload, dict):
        return LogLine(service=service, message=text, raw=raw.rstrip("\r\n"))

    return LogLine(
        timestamp=payload.get("timestamp"),
        service=payload.get("service") or service,
        level=payload.get("level"),
        message=str(payload.get("message", "")),
        trace_id=payload.get("trace_id"),
        extra={k: v for k, v in payload.items() if k not in _KNOWN_FIELDS},
    )


def matches(line: LogLine, q: LogsQuery) -> bool:
    """True when ``line`` satisfies every filter set on ``q``."""
    if q.service and line.service != q.service:
        return False

    if q.levels:
        # An unparsed line has no level, so it cannot satisfy a level filter.
        if line.level is None:
            return False
        if line.level.upper() not in {lvl.upper() for lvl in q.levels}:
            return False

    if q.trace_id and line.trace_id != q.trace_id:
        return False

    if q.contains:
        haystack = (line.raw or line.message).lower()
        if q.contains.lower() not in haystack:
            return False

    return True


def _sort_key(line: LogLine):
    """Order by timestamp, keeping undated lines together at the start."""
    return (line.timestamp is not None, line.timestamp or datetime.min)


def aggregate(lines: list[LogLine], q: LogsQuery) -> LogsAggregate:
    """Count the full match set, then sample the most recent ``limit`` lines."""
    # Counted before filtering: a level filter would hide them, but a spike in
    # non-JSON output is itself the signature of a cascade that never reaches
    # the metrics middleware (see the api-gateway 5xx tech debt).
    unparsed = [line for line in lines if line.raw is not None]
    if q.service:
        unparsed = [line for line in unparsed if line.service == q.service]

    matched = [line for line in lines if matches(line, q)]
    matched.sort(key=_sort_key)

    level_counts = Counter(line.level for line in matched if line.level)
    message_counts = Counter(
        (line.service, line.level, line.message)
        for line in matched
        if line.raw is None
    )

    notes: list[str] = []
    truncated = len(matched) > q.limit
    if truncated:
        notes.append(
            f"{len(matched)} lines matched; returning the {q.limit} most recent."
        )
    if unparsed:
        notes.append(
            f"{len(unparsed)} non-JSON line(s) in the window (uvicorn access log "
            f"or a traceback); counted, with up to {MAX_UNPARSED_SAMPLES} sampled."
        )

    return LogsAggregate(
        lines=matched[-q.limit:],
        total_matched=len(matched),
        # Over the full match set, not the sample: with limit=5 the sample can
        # easily miss a service that the other 39 lines came from.
        services_seen=sorted({line.service for line in matched}),
        level_counts=dict(level_counts.most_common()),
        message_counts=[
            MessageCount(service=service, level=level, message=message, count=count)
            for (service, level, message), count in
            message_counts.most_common(MAX_MESSAGE_COUNTS)
        ],
        unparsed_count=len(unparsed),
        unparsed_samples=sorted(unparsed, key=_sort_key)[-MAX_UNPARSED_SAMPLES:],
        truncated=truncated,
        notes=notes,
    )


def _scope(q: LogsQuery) -> str:
    bits = [f"{k}={v}" for k, v in (
        ("service", q.service),
        ("levels", ",".join(q.levels) if q.levels else None),
        ("contains", q.contains),
        ("trace_id", q.trace_id),
    ) if v]
    return " ".join(bits) if bits else "all services, all levels"


def summarise_logs(q: LogsQuery, agg: LogsAggregate) -> str:
    """One line, written for the LLM to reason on."""
    # Unparsed lines match only when no level/trace filter excludes them, so
    # split them: "also present" would wrongly imply they are additional to the
    # match count when in fact they are part of it.
    matched_unparsed = agg.total_matched - sum(agg.level_counts.values())
    excluded_unparsed = agg.unparsed_count - matched_unparsed

    unparsed_clause = ""
    if matched_unparsed:
        unparsed_clause += (
            f" {matched_unparsed} of those are non-JSON "
            f"(uvicorn access log or a traceback)."
        )
    if excluded_unparsed:
        unparsed_clause += (
            f" A further {excluded_unparsed} non-JSON line(s) in the window were "
            f"excluded by the filters."
        )

    if agg.total_matched == 0:
        return (
            f"No matching log lines for {_scope(q)} in the last "
            f"{q.lookback_minutes}m.{unparsed_clause}"
        )

    services = len(agg.services_seen)
    scale = (
        f"{agg.total_matched} matching line(s) across {services} service(s) in "
        f"{q.lookback_minutes}m"
    )

    # Everything matched was non-JSON: a traceback storm looks exactly like this,
    # and it is the one cascade that never reaches the metrics middleware.
    if not agg.level_counts and not agg.message_counts:
        return (
            f"{scale}, all of them non-JSON (uvicorn access log or a traceback)."
        )

    levels = ", ".join(f"{level} {count}" for level, count in agg.level_counts.items())
    top = "; ".join(
        f'{mc.service}: {mc.count}x "{mc.message}"'
        for mc in agg.message_counts[:3]
    )
    level_clause = f" ({levels})" if levels else ""
    top_clause = f" Top: {top}." if top else ""
    return f"{scale}{level_clause}.{top_clause}{unparsed_clause}"


def resolve_window(
    q: LogsQuery, *, now: Callable[[], datetime] = utc_now
) -> TimeWindow:
    """Turn the query's time fields into a concrete window."""
    if q.since is None:
        return TimeWindow.lookback(q.lookback_minutes, now=now)
    return TimeWindow(start=q.since, end=q.until or now())


# ── reading the containers ───────────────────────────────────────────


def _connect():
    """Build a Docker client from the environment.

    The ``docker`` import is deliberately **inside** the function so that
    ``import agent.tools.logs`` works on a machine with no Docker SDK and no
    socket — the same "imports never fail" rule that lets api/app/config.py
    default DATABASE_URL to "".
    """
    import docker

    return docker.from_env()


def _read_containers(client, project: str, services: list[str], window: TimeWindow):
    """Read and parse each service's stdout. Synchronous — call via to_thread.

    Returns ``(lines, notes)``. A container that cannot be read costs a note,
    not the whole call: if downstream-dep is restarting mid-incident the other
    two services still hold the most valuable evidence, and "downstream-dep is
    restarting" is itself a finding.
    """
    labels = [f"{PROJECT_LABEL}={project}"]
    if len(services) == 1:
        labels.append(f"{SERVICE_LABEL}={services[0]}")

    # all=True: a stopped container's logs are often the whole story.
    containers = client.containers.list(all=True, filters={"label": labels})

    lines: list[LogLine] = []
    notes: list[str] = []
    seen: set[str] = set()

    for container in containers:
        service = container.labels.get(SERVICE_LABEL)
        if service not in services:
            continue  # the project label also matches prometheus and agent-api
        seen.add(service)

        try:
            body = container.logs(
                since=window.unix_start,
                until=window.unix_end,
                tail=MAX_TAIL,
                stdout=True,
                stderr=True,
            )
        except Exception as exc:  # noqa: BLE001 — partial evidence beats none
            notes.append(f"could not read logs for '{service}': {exc}")
            continue

        status = getattr(container, "status", "running")
        if status != "running":
            notes.append(f"container for '{service}' is not running (status={status})")

        text = body.decode("utf-8", errors="replace")
        raw_lines = text.splitlines()
        if len(raw_lines) >= MAX_TAIL:
            notes.append(
                f"'{service}' hit the {MAX_TAIL}-line ceiling; older lines in the "
                f"window were not read."
            )
        for raw in raw_lines:
            line = parse_line(raw, service)
            if line is not None:
                lines.append(line)

    for service in services:
        if service not in seen:
            # A missing container is evidence, not a tool failure.
            notes.append(f"no container for '{service}' in compose project '{project}'")

    return lines, notes


def _query_text(q: LogsQuery, window: TimeWindow) -> str:
    """The literal filter set, as the citation for this observation."""
    levels = ",".join(q.levels) if q.levels else "*"
    return (
        f"service={q.service or '*'} levels=[{levels}] "
        f"contains={q.contains or '*'} trace_id={q.trace_id or '*'} "
        f"since={window.start.isoformat()} until={window.end.isoformat()}"
    )


async def query_logs(
    q: LogsQuery,
    *,
    docker_client=None,
    now: Callable[[], datetime] = utc_now,
    project: str | None = None,
) -> LogsResult:
    """Read structured logs from the victim containers' stdout.

    ``docker-py`` is synchronous, so the read happens in a worker thread. Note
    that it interprets a naive ``datetime`` as **local** time, which would shift
    the whole window by the host's offset — TimeWindow.unix_start/unix_end are
    ints precisely so that cannot happen.
    """
    started = time.perf_counter()
    project = project or config.COMPOSE_PROJECT
    window = resolve_window(q, now=now)
    source = f"docker logs (compose project '{project}')"
    query_text = _query_text(q, window)

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    if q.service is not None and q.service not in config.LOG_SERVICES:
        return failure(
            tool=TOOL_NAME, source=source, query=query_text, window=window,
            error=(
                f"unknown service '{q.service}'; known: {sorted(config.LOG_SERVICES)}"
            ),
            summary=(
                f"No such service '{q.service}'. Logs are available for "
                f"{', '.join(config.LOG_SERVICES)}."
            ),
            model=LogsResult, latency_ms=_elapsed_ms(),
        )

    services = [q.service] if q.service else list(config.LOG_SERVICES)

    try:
        client = docker_client
        if client is None:
            client = await asyncio.to_thread(_connect)
        lines, container_notes = await asyncio.to_thread(
            _read_containers, client, project, services, window
        )
    except Exception as exc:  # noqa: BLE001 — DockerException needs the lazy import
        return failure(
            tool=TOOL_NAME, source=source, query=query_text, window=window,
            error=(
                f"docker api unavailable ({exc}); reading container logs requires "
                f"/var/run/docker.sock to be mounted into this container"
            ),
            summary=(
                "Could not reach the Docker API, so no log evidence is available; "
                "use query_metrics instead."
            ),
            model=LogsResult, latency_ms=_elapsed_ms(),
        )

    agg = aggregate(lines, q)
    return LogsResult(
        tool=TOOL_NAME,
        summary=summarise_logs(q, agg),
        source=source,
        query=query_text,
        window=window,
        truncated=agg.truncated,
        notes=container_notes + agg.notes,
        latency_ms=_elapsed_ms(),
        lines=agg.lines,
        total_matched=agg.total_matched,
        services_seen=agg.services_seen,
        level_counts=agg.level_counts,
        message_counts=agg.message_counts,
        unparsed_count=agg.unparsed_count,
        unparsed_samples=agg.unparsed_samples,
    )
