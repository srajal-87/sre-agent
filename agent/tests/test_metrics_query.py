"""The PromQL builder: pure, no I/O."""

from datetime import datetime, timedelta, timezone

import pytest

from agent.tools.metrics import (
    KNOWN_METRICS,
    MetricsQuery,
    build_query,
    resolve_window,
)

FIXED = datetime(2026, 8, 20, 14, 9, 30, tzinfo=timezone.utc)


def _clock() -> datetime:
    return FIXED


# ── the metric catalogue ─────────────────────────────────────────────

def test_catalogue_matches_the_six_metrics_the_stack_exposes():
    assert set(KNOWN_METRICS) == {
        "http_requests_total",
        "http_request_duration_seconds",
        "upstream_timeouts_total",
        "config_errors_total",
        "config_version",
        "downstream_memory_bytes",
    }


def test_every_catalogue_entry_declares_type_labels_and_services():
    for name, spec in KNOWN_METRICS.items():
        assert spec["type"] in {"counter", "histogram", "gauge"}, name
        assert isinstance(spec["labels"], list), name
        assert spec["services"], name


# ── selectors ────────────────────────────────────────────────────────

def test_service_becomes_an_instance_matcher():
    query = build_query(MetricsQuery(metric="config_version", service="data-service"))
    assert 'instance="data-service:8000"' in query


def test_filters_are_combined_into_one_selector():
    query = build_query(
        MetricsQuery(
            metric="http_requests_total",
            service="api-gateway",
            path="/request",
            status="504",
        )
    )
    assert 'instance="api-gateway:8000"' in query
    assert 'path="/request"' in query
    assert 'status="504"' in query


def test_no_filters_yields_a_bare_metric_name():
    query = build_query(MetricsQuery(metric="config_version", aggregation="last"))
    assert "{}" not in query
    assert "config_version" in query


def test_label_values_are_escaped():
    query = build_query(MetricsQuery(metric="http_requests_total", path='/a"b\\c'))
    assert r'path="/a\"b\\c"' in query


# ── aggregations per metric type ─────────────────────────────────────

def test_counter_rate_wraps_in_rate_and_sums_by_labels():
    query = build_query(
        MetricsQuery(metric="http_requests_total", service="api-gateway", aggregation="rate")
    )
    assert query.startswith("sum by (")
    assert "rate(http_requests_total{" in query


def test_histogram_p99_uses_histogram_quantile_over_bucket():
    query = build_query(
        MetricsQuery(
            metric="http_request_duration_seconds",
            service="downstream-dep",
            path="/data",
            aggregation="p99",
        )
    )
    assert query.startswith("histogram_quantile(0.99, ")
    assert "http_request_duration_seconds_bucket{" in query
    assert "le" in query


def test_histogram_p95_uses_the_matching_quantile():
    query = build_query(
        MetricsQuery(metric="http_request_duration_seconds", aggregation="p95")
    )
    assert query.startswith("histogram_quantile(0.95, ")


def test_histogram_avg_is_sum_over_count():
    query = build_query(
        MetricsQuery(
            metric="http_request_duration_seconds",
            service="data-service",
            aggregation="avg",
        )
    )
    assert "_sum{" in query and "_count{" in query and " / " in query


def test_gauge_last_is_the_bare_series():
    query = build_query(MetricsQuery(metric="downstream_memory_bytes", aggregation="last"))
    assert query == "downstream_memory_bytes"


def test_the_default_aggregation_is_derived_from_the_metric_type():
    """One fixed default can't work: 'rate' is meaningless for a gauge."""
    assert build_query(MetricsQuery(metric="config_version")) == "config_version"
    assert build_query(MetricsQuery(metric="http_requests_total")).startswith("sum by (")
    assert build_query(
        MetricsQuery(metric="http_request_duration_seconds")
    ).startswith("histogram_quantile(0.99, ")


def test_rate_window_is_at_least_a_minute_and_never_below_the_step():
    """A rate window shorter than the step silently skips samples."""
    fast = build_query(MetricsQuery(metric="http_requests_total", step_seconds=30))
    assert "[1m]" in fast

    slow = build_query(MetricsQuery(metric="http_requests_total", step_seconds=300))
    assert "[5m]" in slow


# ── validation ───────────────────────────────────────────────────────

def test_unknown_metric_is_rejected_by_name_with_the_known_list():
    with pytest.raises(ValueError) as excinfo:
        build_query(MetricsQuery(metric="cpu_seconds_total"))
    assert "cpu_seconds_total" in str(excinfo.value)
    assert "http_requests_total" in str(excinfo.value)


def test_aggregation_must_match_the_metric_type():
    with pytest.raises(ValueError) as excinfo:
        build_query(MetricsQuery(metric="http_requests_total", aggregation="p99"))
    assert "p99" in str(excinfo.value)
    assert "rate" in str(excinfo.value)  # names the valid options


def test_a_service_that_does_not_expose_the_metric_is_rejected():
    """Otherwise this returns empty and reads as 'no problem'."""
    with pytest.raises(ValueError) as excinfo:
        build_query(MetricsQuery(metric="config_errors_total", service="api-gateway"))
    assert "data-service" in str(excinfo.value)


def test_a_label_the_metric_does_not_have_is_rejected():
    with pytest.raises(ValueError) as excinfo:
        build_query(MetricsQuery(metric="config_version", status="200"))
    assert "status" in str(excinfo.value)


# ── window resolution ────────────────────────────────────────────────

def test_lookback_minutes_resolves_against_the_injected_clock():
    window = resolve_window(MetricsQuery(metric="config_version", lookback_minutes=15), now=_clock)
    assert window.end == FIXED
    assert window.start == FIXED - timedelta(minutes=15)


def test_absolute_since_and_until_win_over_lookback():
    since = FIXED - timedelta(hours=3)
    window = resolve_window(
        MetricsQuery(metric="config_version", since=since, until=FIXED, lookback_minutes=5),
        now=_clock,
    )
    assert window.start == since
    assert window.end == FIXED


def test_since_alone_runs_up_to_now():
    since = FIXED - timedelta(minutes=42)
    window = resolve_window(MetricsQuery(metric="config_version", since=since), now=_clock)
    assert window.start == since
    assert window.end == FIXED


def test_lookback_and_step_are_clamped_not_rejected():
    """An LLM will send silly numbers; clamp rather than fail the call."""
    wide = MetricsQuery(metric="config_version", lookback_minutes=9999, step_seconds=1)
    assert wide.lookback_minutes == 120
    assert wide.step_seconds == 5

    narrow = MetricsQuery(metric="config_version", lookback_minutes=0, step_seconds=9999)
    assert narrow.lookback_minutes == 1
    assert narrow.step_seconds == 300
