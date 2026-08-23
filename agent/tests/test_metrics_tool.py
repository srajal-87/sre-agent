"""query_metrics end to end, with the HTTP call injected. No network."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from agent.tools.metrics import (
    MetricsQuery,
    MetricsResult,
    build_query,
    query_metrics,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIXED = datetime(2026, 8, 20, 14, 9, 30, tzinfo=timezone.utc)
BASE_URL = "http://prometheus:9090"


def _clock() -> datetime:
    return FIXED


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _returns(payload: dict, record: list | None = None):
    """A fetch that always returns ``payload``, recording its arguments."""

    async def fetch(url: str, params: dict) -> dict:
        if record is not None:
            record.append((url, params))
        return payload

    return fetch


def _raises(exc: Exception):
    async def fetch(url: str, params: dict) -> dict:
        raise exc

    return fetch


def _run(q: MetricsQuery, fetch, **kwargs) -> MetricsResult:
    return asyncio.run(
        query_metrics(q, fetch=fetch, now=_clock, base_url=BASE_URL, **kwargs)
    )


# ── happy path ───────────────────────────────────────────────────────

def test_returns_the_parsed_series_with_a_citable_query():
    q = MetricsQuery(metric="http_requests_total", service="api-gateway")
    result = _run(q, _returns(_fixture("prom_range_counter_rate")))

    assert result.ok is True
    assert result.tool == "query_metrics"
    assert result.series_count == 1
    assert result.query == build_query(q)
    assert result.error is None
    assert "prometheus" in result.source


def test_the_window_comes_from_the_injected_clock():
    q = MetricsQuery(metric="http_requests_total", lookback_minutes=15)
    result = _run(q, _returns(_fixture("prom_range_counter_rate")))

    assert result.window.end == FIXED
    assert result.window.duration_minutes == 15.0


def test_prometheus_is_called_with_unix_bounds_and_a_step():
    calls = []
    q = MetricsQuery(metric="http_requests_total", step_seconds=30)
    _run(q, _returns(_fixture("prom_range_counter_rate"), calls))

    url, params = calls[0]
    assert url == "http://prometheus:9090/api/v1/query_range"
    assert params["query"] == build_query(q)
    assert isinstance(params["start"], int)
    assert isinstance(params["end"], int)
    assert params["end"] - params["start"] == 900
    assert params["step"] == "30s"


def test_latency_is_recorded():
    q = MetricsQuery(metric="http_requests_total")
    result = _run(q, _returns(_fixture("prom_range_counter_rate")))
    assert isinstance(result.latency_ms, int)
    assert result.latency_ms >= 0


def test_notes_and_truncation_reach_the_result():
    q = MetricsQuery(metric="http_request_duration_seconds", service="downstream-dep")
    result = _run(q, _returns(_fixture("prom_range_histogram_p99")))
    assert any("NaN" in n for n in result.notes)


def test_the_result_survives_a_strict_json_dump_for_the_evidence_column():
    q = MetricsQuery(metric="http_request_duration_seconds", service="downstream-dep")
    result = _run(q, _returns(_fixture("prom_range_histogram_p99")))
    json.dumps(result.model_dump(mode="json"), allow_nan=False)


# ── empty is not failure ─────────────────────────────────────────────

def test_an_empty_result_is_a_success_with_zero_series():
    q = MetricsQuery(metric="http_requests_total", service="api-gateway", path="/nonexistent")
    result = _run(q, _returns(_fixture("prom_range_empty")))

    assert result.ok is True
    assert result.series == []
    assert result.series_count == 0
    assert "/nonexistent" in result.summary


# ── expected failures never raise ────────────────────────────────────

def test_a_prometheus_error_payload_is_reported_verbatim():
    q = MetricsQuery(metric="http_requests_total")
    result = _run(q, _returns(_fixture("prom_range_error")))

    assert result.ok is False
    assert "parse error" in result.error
    assert isinstance(result, MetricsResult)


def test_an_unreachable_prometheus_names_the_url_and_suggests_logs():
    q = MetricsQuery(metric="http_requests_total")
    result = _run(q, _raises(httpx.ConnectError("connection refused")))

    assert result.ok is False
    assert "prometheus:9090" in result.error
    assert "query_logs" in result.summary


def test_a_timeout_is_reported_as_a_timeout_not_a_connection_failure():
    q = MetricsQuery(metric="http_requests_total")
    result = _run(q, _raises(httpx.ReadTimeout("too slow")))

    assert result.ok is False
    assert "timed out" in result.error
    assert result.window is not None  # the attempted range is still citable


def test_an_unknown_metric_returns_an_error_result_rather_than_raising():
    q = MetricsQuery(metric="cpu_seconds_total")
    result = _run(q, _returns({}))

    assert result.ok is False
    assert "cpu_seconds_total" in result.error
    assert "http_requests_total" in result.error  # names the valid options
    assert result.query == ""  # nothing was issued, so nothing to cite


def test_an_invalid_aggregation_returns_an_error_result():
    q = MetricsQuery(metric="config_version", aggregation="rate")
    result = _run(q, _returns({}))

    assert result.ok is False
    assert "last" in result.error


def test_a_non_json_body_is_an_error_not_a_crash():
    q = MetricsQuery(metric="http_requests_total")
    result = _run(q, _raises(ValueError("Expecting value: line 1 column 1")))

    assert result.ok is False
    assert result.ok is not None
