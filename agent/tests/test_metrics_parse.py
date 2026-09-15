"""Parsing a real Prometheus query_range payload. Fixtures are captured, not written."""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.tools.metrics import (
    MAX_POINTS,
    MAX_SERIES,
    MetricsQuery,
    parse_range_response,
    summarise,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


MATRIX_BASE_TS = 1787223249
MATRIX_STEP_SECONDS = 60


def _matrix(*labelled: tuple[str, list[float | None]]) -> dict:
    """A query_range payload, written as data rather than as JSON literals.

    ``None`` renders as Prometheus' string "NaN", which is how a series that has
    stopped producing samples actually comes back.
    """
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"instance": f"{label}:8000"},
                    "values": [
                        [MATRIX_BASE_TS + i * MATRIX_STEP_SECONDS,
                         "NaN" if v is None else str(v)]
                        for i, v in enumerate(values)
                    ],
                }
                for label, values in labelled
            ],
        },
    }


def _requested_end(raw: dict) -> datetime:
    """The end of the window Prometheus was asked about.

    Every step Prometheus was asked for comes back in ``values``, NaN included,
    so the last requested timestamp is the last one in the payload - which is
    exactly what a series has to keep up with to count as still reporting.
    """
    last = max(float(ts) for r in raw["data"]["result"] for ts, _ in r["values"])
    return datetime.fromtimestamp(last, tz=timezone.utc)


# ── happy path ───────────────────────────────────────────────────────

def test_parses_a_counter_rate_payload_into_series():
    series, truncated, notes = parse_range_response(_fixture("prom_range_counter_rate"))
    assert len(series) == 1
    assert series[0].labels["instance"] == "api-gateway:8000"
    assert len(series[0].points) == 18


def test_points_carry_utc_timestamps_and_float_values():
    series, _, _ = parse_range_response(_fixture("prom_range_gauge"))
    point = series[0].points[0]
    assert point.ts.utcoffset().total_seconds() == 0
    assert isinstance(point.value, float)


def test_stats_are_computed_over_the_kept_points():
    series, _, _ = parse_range_response(_fixture("prom_range_counter_rate"))
    s = series[0]
    values = [p.value for p in s.points]
    assert s.min == min(values)
    assert s.max == max(values)
    assert s.latest == values[-1]
    assert s.mean == pytest.approx(sum(values) / len(values))


# ── the NaN trap ─────────────────────────────────────────────────────

def test_nan_points_are_dropped_not_carried_as_float_nan():
    """histogram_quantile over empty buckets returns the *string* "NaN"."""
    raw = _fixture("prom_range_histogram_p99")
    nan_count = sum(
        1 for r in raw["data"]["result"] for _, v in r["values"] if v == "NaN"
    )
    assert nan_count > 0, "fixture must actually contain NaN to be meaningful"

    series, _, notes = parse_range_response(raw)
    values = [p.value for s in series for p in s.points]
    assert values
    assert not any(math.isnan(v) for v in values)
    assert len(values) == 18 - nan_count
    assert any("NaN" in n for n in notes)


def test_a_series_that_is_entirely_nan_is_dropped_with_a_note():
    raw = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"instance": "x:8000"}, "values": [[1, "NaN"], [2, "NaN"]]}],
        },
    }
    series, _, notes = parse_range_response(raw)
    assert series == []
    assert any("NaN" in n for n in notes)


def test_parsed_series_survive_a_strict_json_dump():
    """float('nan') would break json.dumps and poison the evidence jsonb write."""
    series, _, _ = parse_range_response(_fixture("prom_range_histogram_p99"))
    json.dumps([s.model_dump(mode="json") for s in series], allow_nan=False)


# ── empty ────────────────────────────────────────────────────────────

def test_an_empty_result_set_parses_to_no_series_without_error():
    series, truncated, notes = parse_range_response(_fixture("prom_range_empty"))
    assert series == []
    assert truncated is False


# ── truncation ───────────────────────────────────────────────────────

def test_series_beyond_the_cap_are_dropped_and_flagged():
    raw = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"instance": f"svc-{i}:8000"}, "values": [[1, "1.0"]]}
                for i in range(MAX_SERIES + 5)
            ],
        },
    }
    series, truncated, notes = parse_range_response(raw)
    assert len(series) == MAX_SERIES
    assert truncated is True
    assert any(str(MAX_SERIES + 5) in n for n in notes)


def test_long_series_are_downsampled_keeping_the_endpoints():
    """The latest point is the one the agent reasons about; it must survive."""
    values = [[i, str(float(i))] for i in range(MAX_POINTS * 3)]
    raw = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"instance": "x:8000"}, "values": values}],
        },
    }
    series, truncated, notes = parse_range_response(raw)
    assert len(series[0].points) <= MAX_POINTS
    assert truncated is True
    assert series[0].points[-1].value == float(MAX_POINTS * 3 - 1)
    assert series[0].points[0].value == 0.0


def test_stats_are_computed_before_downsampling():
    """min/max must reflect the real data, not the sampled subset."""
    values = [[i, "1.0"] for i in range(MAX_POINTS * 3)]
    values[7] = [7, "999.0"]  # a spike that downsampling would likely drop
    raw = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"instance": "x:8000"}, "values": values}],
        },
    }
    series, _, _ = parse_range_response(raw)
    assert series[0].max == 999.0


# ── summary ──────────────────────────────────────────────────────────

def test_summary_names_the_metric_service_and_movement():
    raw = _fixture("prom_range_histogram_p99")
    q = MetricsQuery(
        metric="http_request_duration_seconds", service="downstream-dep", path="/data"
    )
    series, _, _ = parse_range_response(raw)
    text = summarise(q, series, window_end=_requested_end(raw))
    assert "http_request_duration_seconds" in text
    assert "downstream-dep" in text
    assert "p99" in text


def test_summary_for_no_series_states_what_matched_nothing():
    """Absence is evidence — the summary must say what was looked for."""
    q = MetricsQuery(metric="http_requests_total", service="api-gateway", path="/nonexistent")
    text = summarise(q, [], window_end=datetime.now(timezone.utc))
    assert "no" in text.lower()
    assert "http_requests_total" in text
    assert "/nonexistent" in text


def test_summary_reports_a_flat_series_as_flat():
    raw = _fixture("prom_range_gauge")
    q = MetricsQuery(metric="config_version", service="data-service")
    series, _, _ = parse_range_response(raw)
    text = summarise(q, series, window_end=_requested_end(raw))
    assert text  # a single sentence, always
    assert "\n" not in text
    assert "flat at" in text


def test_float_noise_is_not_described_as_movement():
    """0.00495 vs 0.0049499999999999995 is a constant series, not a change.

    Read at the last point it actually reported, so the constancy is what is
    under test rather than the staleness.
    """
    raw = _fixture("prom_range_histogram_p99")
    q = MetricsQuery(metric="http_request_duration_seconds", service="downstream-dep")
    series, _, _ = parse_range_response(raw)
    text = summarise(q, series, window_end=series[0].points[-1].ts)
    assert "flat at" in text
    assert "from" not in text


# ── the stale series ─────────────────────────────────────────────────

def test_a_series_that_stopped_reporting_says_so_instead_of_reporting_a_stale_value():
    """The strongest refutation must not arrive disguised as reassurance. With
    no samples, histogram_quantile returns NaN, the NaN points are dropped, and
    the surviving pre-fault points describe a calm, healthy trend that is two
    minutes out of date."""
    raw = _fixture("prom_range_histogram_p99")
    q = MetricsQuery(metric="http_request_duration_seconds", service="downstream-dep")
    series, _, _ = parse_range_response(raw)
    text = summarise(q, series, window_end=_requested_end(raw))

    assert "stopped reporting" in text
    assert "10:55:39" in text          # the last point that actually reported
    assert "flat at" not in text       # ... and no trend claimed for a dead series
    assert "fell from" not in text


def test_a_series_still_reporting_is_described_as_a_trend():
    """The disclosure must not swallow the normal case."""
    raw = _fixture("prom_range_counter_rate")
    q = MetricsQuery(metric="http_requests_total", service="api-gateway")
    series, _, _ = parse_range_response(raw)
    text = summarise(q, series, window_end=_requested_end(raw))

    assert "stopped reporting" not in text
    assert "fell from" in text


# ── which series a multi-series summary describes ────────────────────

def test_a_multi_series_summary_describes_the_biggest_movers_not_the_first():
    """Prometheus' series order is arbitrary, and from turn 2 the summary line
    is all the model still sees. Describing series[0] means the evidence it
    reasons on is whichever series the TSDB happened to list first."""
    raw = _matrix(
        ("noise-a", [1.0, 1.0, 1.0]),
        ("mover", [5.0, 3.0, 0.0]),       # the largest change, listed second
        ("noise-b", [2.0, 2.0, 2.01]),
        ("noise-c", [0.5, 0.5, 0.5]),
    )
    series, _, _ = parse_range_response(raw)
    text = summarise(
        MetricsQuery(metric="http_requests_total"), series,
        window_end=_requested_end(raw),
    )

    assert "mover" in text
    assert "fell from 5 to 0" in text


def test_a_multi_series_summary_describes_at_most_three():
    """Bounded: the summary is one line the model reads on every later turn."""
    raw = _matrix(*((f"svc-{i}", [float(i), 0.0]) for i in range(8)))
    series, _, _ = parse_range_response(raw)
    text = summarise(
        MetricsQuery(metric="http_requests_total"), series,
        window_end=_requested_end(raw),
    )

    assert text.count(";") == 2          # three clauses
    assert "svc-7" in text and "svc-6" in text and "svc-5" in text
    assert "svc-0" not in text
    assert "8 series" in text            # the rest still accounted for


def test_a_series_that_stopped_reporting_outranks_one_that_merely_moved():
    """Step 1's disclosure must survive Step 2's selection: a series that went
    silent is a bigger event than one that changed value."""
    raw = _matrix(
        ("big-mover", [100.0, 50.0, 0.0]),
        ("went-silent", [1.0, None, None]),
    )
    series, _, _ = parse_range_response(raw)
    text = summarise(
        MetricsQuery(metric="http_requests_total"), series,
        window_end=_requested_end(raw),
    )

    assert text.index("went-silent") < text.index("big-mover")
    assert "stopped reporting" in text


def test_the_ordering_does_not_depend_on_the_order_prometheus_returned():
    """Deterministic, so two identical investigations read identical evidence."""
    pairs = [("a", [1.0, 9.0]), ("b", [1.0, 5.0]), ("c", [1.0, 2.0])]
    q = MetricsQuery(metric="http_requests_total")
    forward, _, _ = parse_range_response(_matrix(*pairs))
    backward, _, _ = parse_range_response(_matrix(*reversed(pairs)))
    end = _requested_end(_matrix(*pairs))

    assert summarise(q, forward, window_end=end) == summarise(
        q, backward, window_end=end
    )


def test_summaries_and_notes_are_ascii():
    """probe.py prints these to a Windows console; a stray em-dash mangles."""
    raw = _fixture("prom_range_histogram_p99")
    series, _, notes = parse_range_response(raw)
    end = _requested_end(raw)
    q = MetricsQuery(metric="http_request_duration_seconds", service="downstream-dep")
    for text in [summarise(q, series, window_end=end),
                 summarise(q, [], window_end=end), *notes]:
        text.encode("ascii")
