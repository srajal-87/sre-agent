"""query_logs end to end, with the Docker client injected. No socket needed."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from agent.tools.logs import LogsQuery, LogsResult, query_logs

FIXTURES = Path(__file__).parent / "fixtures"
FIXED = datetime(2026, 8, 20, 14, 9, 30, tzinfo=timezone.utc)
FAULT_TRACE = "4c5705ee-7292-41ef-af3f-037a21811a8f"


def _clock() -> datetime:
    return FIXED


class _FakeContainer:
    def __init__(self, service: str, body: bytes, status: str = "running"):
        self.labels = {
            "com.docker.compose.project": "sre-agent",
            "com.docker.compose.service": service,
        }
        self.name = f"sre-agent-{service}-1"
        self.status = status
        self._body = body
        self.log_calls = []

    def logs(self, **kwargs) -> bytes:
        self.log_calls.append(kwargs)
        return self._body


class _FakeDocker:
    """Stands in for docker.DockerClient."""

    def __init__(self, containers, list_error: Exception | None = None):
        self._containers = containers
        self._list_error = list_error
        self.list_calls = []

    @property
    def containers(self):
        return self

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if self._list_error:
            raise self._list_error
        wanted = kwargs.get("filters", {}).get("label", [])
        return [
            c
            for c in self._containers
            if all(
                label in {f"{k}={v}" for k, v in c.labels.items()} for label in wanted
            )
        ]


def _captured(service: str) -> bytes:
    return (FIXTURES / f"docker_logs_{service}.txt").read_bytes()


def _fake_stack() -> _FakeDocker:
    return _FakeDocker(
        [
            _FakeContainer(s, _captured(s))
            for s in ("api-gateway", "data-service", "downstream-dep")
        ]
    )


def _run(q: LogsQuery, client) -> LogsResult:
    return asyncio.run(query_logs(q, docker_client=client, now=_clock))


# ── happy path ───────────────────────────────────────────────────────

def test_reads_all_three_services_and_counts_the_fault():
    result = _run(LogsQuery(levels=["ERROR", "WARNING"], limit=5), _fake_stack())

    assert result.ok is True
    assert result.tool == "query_logs"
    assert result.total_matched == 44
    assert result.level_counts == {"WARNING": 22, "ERROR": 22}
    assert len(result.lines) == 5


def test_the_query_string_describes_the_filters_for_citation():
    result = _run(LogsQuery(levels=["ERROR"], contains="timeout"), _fake_stack())

    assert "ERROR" in result.query
    assert "timeout" in result.query
    assert result.source


def test_the_window_comes_from_the_injected_clock():
    result = _run(LogsQuery(lookback_minutes=5), _fake_stack())

    assert result.window.end == FIXED
    assert result.window.duration_minutes == 5.0


def test_docker_is_given_integer_unix_bounds_not_datetimes():
    """docker-py reads a naive datetime as LOCAL time, shifting the whole window."""
    stack = _fake_stack()
    _run(LogsQuery(lookback_minutes=15), stack)

    kwargs = stack._containers[0].log_calls[0]
    assert isinstance(kwargs["since"], int)
    assert isinstance(kwargs["until"], int)
    assert kwargs["until"] - kwargs["since"] == 900
    assert kwargs["stdout"] is True and kwargs["stderr"] is True
    assert kwargs["tail"] > 0


def test_containers_are_resolved_by_compose_label_not_by_name():
    """Resolving by name breaks the moment the project is renamed."""
    stack = _fake_stack()
    _run(LogsQuery(service="data-service"), stack)

    labels = stack.list_calls[0]["filters"]["label"]
    assert "com.docker.compose.project=sre-agent" in labels
    assert "com.docker.compose.service=data-service" in labels


def test_a_service_filter_only_reads_that_container():
    stack = _fake_stack()
    result = _run(LogsQuery(service="downstream-dep", levels=["WARNING"]), stack)

    assert result.services_seen == ["downstream-dep"]
    assert result.total_matched == 22


def test_the_trace_filter_returns_the_full_cross_service_chain():
    result = _run(LogsQuery(trace_id=FAULT_TRACE), _fake_stack())

    assert result.services_seen == ["api-gateway", "data-service", "downstream-dep"]
    assert any("injected latency" == l.message for l in result.lines)


def test_the_result_survives_a_strict_json_dump_for_the_evidence_column():
    result = _run(LogsQuery(levels=["ERROR", "WARNING"]), _fake_stack())
    json.dumps(result.model_dump(mode="json"), allow_nan=False)


def test_latency_is_recorded():
    result = _run(LogsQuery(), _fake_stack())
    assert isinstance(result.latency_ms, int)


# ── empty is not failure ─────────────────────────────────────────────

def test_no_matching_lines_is_a_success():
    result = _run(LogsQuery(contains="disk full"), _fake_stack())

    assert result.ok is True
    assert result.lines == []
    assert result.total_matched == 0
    assert "disk full" in result.summary


def test_no_container_for_a_service_is_a_success_with_a_note():
    """A stopped container IS evidence — not a tool failure."""
    stack = _FakeDocker([])
    result = _run(LogsQuery(service="data-service"), stack)

    assert result.ok is True
    assert result.lines == []
    assert any("data-service" in n for n in result.notes)


def test_a_container_that_produced_nothing_is_a_success():
    stack = _FakeDocker([_FakeContainer("data-service", b"")])
    result = _run(LogsQuery(service="data-service"), stack)

    assert result.ok is True
    assert result.total_matched == 0


# ── expected failures never raise ────────────────────────────────────

def test_an_unavailable_socket_names_the_required_mount():
    class _Boom:
        @property
        def containers(self):
            raise RuntimeError("Error while fetching server API version")

    result = _run(LogsQuery(), _Boom())

    assert result.ok is False
    assert "/var/run/docker.sock" in result.error
    assert isinstance(result, LogsResult)


def test_a_docker_api_error_is_reported_not_raised():
    stack = _FakeDocker([], list_error=RuntimeError("500 Server Error"))
    result = _run(LogsQuery(), stack)

    assert result.ok is False
    assert "500 Server Error" in result.error


def test_one_unreadable_container_does_not_lose_the_others():
    """Partial evidence beats no evidence."""

    class _BadContainer(_FakeContainer):
        def logs(self, **kwargs):
            raise RuntimeError("container is restarting")

    stack = _FakeDocker(
        [
            _BadContainer("api-gateway", b""),
            _FakeContainer("downstream-dep", _captured("downstream-dep")),
        ]
    )
    result = _run(LogsQuery(levels=["WARNING"]), stack)

    assert result.ok is True
    assert result.total_matched == 22
    assert any("api-gateway" in n for n in result.notes)


# ── decoding ─────────────────────────────────────────────────────────

def test_undecodable_bytes_do_not_kill_the_read():
    stack = _FakeDocker([_FakeContainer("data-service", b'\xff\xfe bad bytes\n')])
    result = _run(LogsQuery(service="data-service"), stack)

    assert result.ok is True
    assert result.unparsed_count == 1
