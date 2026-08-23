"""The shared tool envelope: TimeWindow, ToolResult, failure()."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent.tools.base import TimeWindow, ToolResult, failure, utc_now

FIXED = datetime(2026, 8, 20, 14, 9, 30, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return FIXED


# ── utc_now ──────────────────────────────────────────────────────────

def test_utc_now_is_timezone_aware_utc():
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


# ── TimeWindow ───────────────────────────────────────────────────────

def test_lookback_ends_now_and_starts_n_minutes_earlier():
    window = TimeWindow.lookback(15, now=_fixed_clock)
    assert window.end == FIXED
    assert window.start == FIXED - timedelta(minutes=15)


def test_lookback_uses_the_real_clock_by_default():
    window = TimeWindow.lookback(5)
    assert (window.end - window.start) == timedelta(minutes=5)


def test_explicit_bounds_are_normalised_to_utc():
    """A tz-aware non-UTC bound is converted, not just relabelled."""
    ist = timezone(timedelta(hours=5, minutes=30))
    window = TimeWindow(start=FIXED.astimezone(ist), end=FIXED)
    assert window.start.utcoffset() == timedelta(0)
    assert window.start == FIXED


def test_a_naive_bound_is_assumed_utc_not_local():
    window = TimeWindow(start=FIXED.replace(tzinfo=None), end=FIXED)
    assert window.start == FIXED


def test_end_before_start_is_rejected():
    with pytest.raises(ValueError):
        TimeWindow(start=FIXED, end=FIXED - timedelta(minutes=1))


def test_unix_bounds_are_ints_for_docker_and_prometheus():
    window = TimeWindow(start=FIXED - timedelta(minutes=5), end=FIXED)
    assert window.unix_start == int((FIXED - timedelta(minutes=5)).timestamp())
    assert window.unix_end == int(FIXED.timestamp())
    assert isinstance(window.unix_start, int)


def test_duration_minutes_is_reported():
    window = TimeWindow.lookback(15, now=_fixed_clock)
    assert window.duration_minutes == 15.0


# ── ToolResult ───────────────────────────────────────────────────────

def test_tool_result_defaults_are_the_success_shape():
    result = ToolResult(
        tool="query_metrics", summary="ok", source="prometheus", query="up"
    )
    assert result.ok is True
    assert result.error is None
    assert result.truncated is False
    assert result.notes == []
    assert result.latency_ms == 0
    assert result.window is None


def test_notes_are_not_shared_between_instances():
    """Field(default_factory=list), not a mutable default."""
    first = ToolResult(tool="t", summary="s", source="src", query="q")
    second = ToolResult(tool="t", summary="s", source="src", query="q")
    first.notes.append("only mine")
    assert second.notes == []


def test_result_is_json_serialisable_for_the_evidence_jsonb_column():
    """model_dump(mode="json") must survive a strict json.dumps."""
    result = ToolResult(
        tool="query_metrics",
        summary="p99 rose to 3.01s",
        source="prometheus @ http://prometheus:9090",
        query="histogram_quantile(0.99, ...)",
        window=TimeWindow.lookback(15, now=_fixed_clock),
        latency_ms=41,
    )
    encoded = json.dumps(result.model_dump(mode="json"), allow_nan=False)
    assert "2026-08-20T14:09:30" in encoded


# ── failure() ────────────────────────────────────────────────────────

def test_failure_builds_the_error_envelope():
    result = failure(
        tool="query_metrics",
        source="prometheus @ http://prometheus:9090",
        query="up",
        error="prometheus unreachable at http://prometheus:9090",
        summary="Could not reach Prometheus; try query_logs instead.",
    )
    assert result.ok is False
    assert result.tool == "query_metrics"
    assert result.error == "prometheus unreachable at http://prometheus:9090"
    assert result.summary.startswith("Could not reach Prometheus")


def test_failure_returns_the_subclass_when_given_one():
    """Each tool's failure path must return *its own* Result type."""

    class MetricsResult(ToolResult):
        series_count: int = 0

    result = failure(
        tool="query_metrics",
        source="prometheus",
        query="up",
        error="boom",
        summary="boom",
        model=MetricsResult,
    )
    assert isinstance(result, MetricsResult)
    assert result.series_count == 0
