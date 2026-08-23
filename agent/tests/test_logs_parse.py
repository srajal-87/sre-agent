"""Parsing and aggregating real container output. Fixtures captured from a live fault."""

from datetime import datetime, timezone
from pathlib import Path

from agent.tools.logs import (
    MAX_MESSAGE_COUNTS,
    MAX_UNPARSED_SAMPLES,
    LogsQuery,
    aggregate,
    matches,
    parse_line,
    summarise_logs,
)

FIXTURES = Path(__file__).parent / "fixtures"

# A trace captured during a real latency fault: it reaches all three services and
# carries both the injected delay and the gateway's timeout.
FAULT_TRACE = "4c5705ee-7292-41ef-af3f-037a21811a8f"


def _all_lines():
    """Every captured line from all three services, parsed."""
    lines = []
    for service in ("api-gateway", "data-service", "downstream-dep"):
        text = (FIXTURES / f"docker_logs_{service}.txt").read_text(encoding="utf-8")
        for raw in text.splitlines():
            line = parse_line(raw, service)
            if line is not None:
                lines.append(line)
    return lines


# ── parse_line ───────────────────────────────────────────────────────

def test_parses_a_structured_json_line():
    raw = (
        '{"timestamp": "2026-08-20T11:19:02.694Z", "service": "downstream-dep", '
        '"level": "WARNING", "message": "injected latency", '
        '"trace_id": "abc", "delay_ms": 3000}'
    )
    line = parse_line(raw, "downstream-dep")

    assert line.service == "downstream-dep"
    assert line.level == "WARNING"
    assert line.message == "injected latency"
    assert line.trace_id == "abc"
    assert line.timestamp == datetime(2026, 8, 20, 11, 19, 2, 694000, tzinfo=timezone.utc)
    assert line.raw is None


def test_structured_extras_are_kept_but_known_fields_are_not_duplicated():
    raw = (
        '{"timestamp": "2026-08-20T11:19:04.733Z", "service": "api-gateway", '
        '"level": "INFO", "message": "request", "trace_id": "abc", '
        '"method": "GET", "path": "/request", "status": 504}'
    )
    line = parse_line(raw, "api-gateway")

    assert line.extra == {"method": "GET", "path": "/request", "status": 504}


def test_a_uvicorn_access_line_is_kept_as_raw():
    raw = 'INFO:     172.19.0.3:41034 - "GET /metrics HTTP/1.1" 200 OK'
    line = parse_line(raw, "api-gateway")

    assert line.raw == raw
    assert line.level is None
    assert line.timestamp is None
    assert line.service == "api-gateway"


def test_a_traceback_line_is_kept_as_raw():
    line = parse_line('  File "/app/app/main.py", line 31, in call_upstream', "api-gateway")
    assert line.raw is not None
    assert line.level is None


def test_malformed_json_is_raw_not_a_crash():
    line = parse_line('{"message": "truncated', "data-service")
    assert line.raw is not None
    assert line.level is None


def test_blank_lines_are_skipped():
    assert parse_line("", "api-gateway") is None
    assert parse_line("   \r", "api-gateway") is None


def test_the_captured_fixtures_contain_both_json_and_non_json():
    lines = _all_lines()
    assert any(line.raw is None for line in lines)
    assert any(line.raw is not None for line in lines)


# ── matches ──────────────────────────────────────────────────────────

def test_level_filter_is_case_insensitive():
    line = parse_line('{"service": "s", "level": "ERROR", "message": "m"}', "s")
    assert matches(line, LogsQuery(levels=["error"]))
    assert not matches(line, LogsQuery(levels=["WARNING"]))


def test_a_level_filter_excludes_unparsed_lines():
    """A uvicorn line has no level, so it cannot satisfy levels=[ERROR]."""
    line = parse_line("INFO:     1.2.3.4 - \"GET / HTTP/1.1\" 200 OK", "api-gateway")
    assert not matches(line, LogsQuery(levels=["ERROR"]))


def test_contains_matches_the_message_case_insensitively():
    line = parse_line('{"service": "s", "level": "ERROR", "message": "upstream timeout"}', "s")
    assert matches(line, LogsQuery(contains="TIMEOUT"))
    assert not matches(line, LogsQuery(contains="config"))


def test_contains_also_searches_unparsed_text():
    line = parse_line("Traceback (most recent call last):", "api-gateway")
    assert matches(line, LogsQuery(contains="traceback"))


def test_trace_id_filter_follows_one_request_across_all_three_hops():
    """This is the causal link the agent must learn to follow."""
    hits = [l for l in _all_lines() if matches(l, LogsQuery(trace_id=FAULT_TRACE))]

    assert {l.service for l in hits} == {"api-gateway", "data-service", "downstream-dep"}
    messages = {l.message for l in hits}
    assert "injected latency" in messages
    assert "upstream timeout calling data-service" in messages


def test_service_filter_narrows_to_one_service():
    lines = _all_lines()
    hits = [l for l in lines if matches(l, LogsQuery(service="data-service"))]
    assert hits
    assert {l.service for l in hits} == {"data-service"}


# ── aggregate ────────────────────────────────────────────────────────

def test_counts_are_computed_over_the_full_match_set_not_the_sample():
    """Aggregate first, sample second — that is what keeps the token cost flat."""
    q = LogsQuery(levels=["ERROR", "WARNING"], limit=5)
    result = aggregate(_all_lines(), q)

    assert result.total_matched == 44  # 22 injected latency + 22 upstream timeout
    assert result.level_counts == {"WARNING": 22, "ERROR": 22}
    assert len(result.lines) == 5


def test_services_seen_covers_the_full_match_set_not_the_sample():
    """With limit=5 the sample can easily miss a service the other 39 came from."""
    result = aggregate(_all_lines(), LogsQuery(trace_id=FAULT_TRACE, limit=2))
    assert result.services_seen == ["api-gateway", "data-service", "downstream-dep"]
    assert len({l.service for l in result.lines}) < 3


def test_the_sample_is_the_most_recent_lines_in_time_order():
    q = LogsQuery(levels=["ERROR", "WARNING"], limit=5)
    result = aggregate(_all_lines(), q)

    stamps = [l.timestamp for l in result.lines]
    assert stamps == sorted(stamps)

    everything = sorted(
        (l for l in _all_lines() if matches(l, q)), key=lambda l: l.timestamp
    )
    assert result.lines[-1].timestamp == everything[-1].timestamp


def test_message_counts_are_the_top_distinct_messages():
    q = LogsQuery(levels=["ERROR", "WARNING"])
    result = aggregate(_all_lines(), q)

    top = result.message_counts[0]
    assert top.count == 22
    assert top.message in {"injected latency", "upstream timeout calling data-service"}
    assert len(result.message_counts) <= MAX_MESSAGE_COUNTS


def test_message_counts_exclude_unparsed_lines():
    """Access-log lines are all distinct; they would flood the histogram."""
    result = aggregate(_all_lines(), LogsQuery())
    assert not any("HTTP/1.1" in mc.message for mc in result.message_counts)


def test_unparsed_lines_are_counted_even_when_a_level_filter_excludes_them():
    """A spike here is the signature of a cascade that never reaches the metrics."""
    result = aggregate(_all_lines(), LogsQuery(levels=["ERROR"]))
    assert result.unparsed_count > 0


def test_a_few_unparsed_lines_are_sampled_with_raw_set():
    result = aggregate(_all_lines(), LogsQuery(levels=["ERROR"]))
    assert 0 < len(result.unparsed_samples) <= MAX_UNPARSED_SAMPLES
    assert all(s.raw for s in result.unparsed_samples)


def test_truncation_is_flagged_when_the_match_set_exceeds_the_limit():
    result = aggregate(_all_lines(), LogsQuery(levels=["ERROR", "WARNING"], limit=5))
    assert result.truncated is True
    assert any("44" in n for n in result.notes)


def test_no_truncation_when_everything_fits():
    result = aggregate(_all_lines(), LogsQuery(trace_id=FAULT_TRACE, limit=200))
    assert result.truncated is False


def test_an_empty_match_set_is_not_an_error():
    result = aggregate(_all_lines(), LogsQuery(contains="no such message anywhere"))
    assert result.lines == []
    assert result.total_matched == 0
    assert result.level_counts == {}


# ── the bad_config cascade ───────────────────────────────────────────

def _bad_config_gateway_lines():
    """api-gateway output captured during a live bad_config fault."""
    text = (FIXTURES / "docker_logs_bad_config_api_gateway.txt").read_text(
        encoding="utf-8"
    )
    return [
        line
        for line in (parse_line(raw, "api-gateway") for raw in text.splitlines())
        if line is not None
    ]


def test_the_bad_config_cascade_leaves_no_structured_error_at_the_gateway():
    """Captured proof of the tech debt: raise_for_status escapes uncaught.

    The gateway's HTTPStatusError bypasses both the metrics middleware and the
    structured logger, so there is no ERROR line and no 500 counted. If this
    ever starts failing, the gateway learned to handle 5xx and the note below
    can go.
    """
    lines = _bad_config_gateway_lines()
    structured_errors = [l for l in lines if l.level == "ERROR"]

    assert structured_errors == []
    assert any("Traceback" in (l.raw or "") for l in lines)


def test_unparsed_count_is_the_only_gateway_signal_for_bad_config():
    """levels=[ERROR] returns nothing, yet the tool must still show something."""
    lines = _bad_config_gateway_lines()
    q = LogsQuery(levels=["ERROR"])
    result = aggregate(lines, q)

    assert result.total_matched == 0
    assert result.unparsed_count > 0
    assert any(s.raw for s in result.unparsed_samples)

    text = summarise_logs(q, result)
    assert "non-JSON" in text
    assert str(result.unparsed_count) in text


# ── summary ──────────────────────────────────────────────────────────

def test_summary_leads_with_the_counts_and_the_top_messages():
    q = LogsQuery(levels=["ERROR", "WARNING"], limit=5)
    result = aggregate(_all_lines(), q)
    text = summarise_logs(q, result)

    assert "44" in text
    assert "injected latency" in text
    assert "upstream timeout calling data-service" in text
    assert "\n" not in text


def test_summary_for_no_matches_says_what_was_looked_for():
    q = LogsQuery(levels=["ERROR"], contains="disk full")
    result = aggregate(_all_lines(), q)
    text = summarise_logs(q, result)

    assert "no" in text.lower()
    assert "disk full" in text


def test_summary_when_every_matched_line_is_non_json():
    """A traceback storm matches nothing structured; the sentence must still read."""
    lines = [l for l in _all_lines() if l.raw is not None]
    q = LogsQuery()
    text = summarise_logs(q, aggregate(lines, q))

    assert "()" not in text
    assert "Top: ." not in text
    assert "all of them non-JSON" in text


def test_summary_calls_out_unparsed_lines():
    q = LogsQuery(levels=["ERROR"])
    result = aggregate(_all_lines(), q)
    assert "non-JSON" in summarise_logs(q, result)


def test_unparsed_lines_inside_the_match_set_are_not_described_as_extra():
    """With no level filter they ARE the match set; "also present" would mislead."""
    q = LogsQuery()
    result = aggregate(_all_lines(), q)
    text = summarise_logs(q, result)

    matched_unparsed = result.total_matched - sum(result.level_counts.values())
    assert matched_unparsed > 0
    assert f"{matched_unparsed} of those are non-JSON" in text
    assert "also present" not in text


def test_unparsed_lines_outside_the_match_set_are_described_as_excluded():
    q = LogsQuery(levels=["ERROR", "WARNING"])
    result = aggregate(_all_lines(), q)
    text = summarise_logs(q, result)

    assert "excluded by the filters" in text
    assert "of those are non-JSON" not in text


def test_summaries_are_ascii():
    q = LogsQuery(levels=["ERROR", "WARNING"])
    result = aggregate(_all_lines(), q)
    summarise_logs(q, result).encode("ascii")
    for note in result.notes:
        note.encode("ascii")


# ── clamping ─────────────────────────────────────────────────────────

def test_lookback_and_limit_are_clamped_not_rejected():
    wide = LogsQuery(lookback_minutes=9999, limit=9999)
    assert wide.lookback_minutes == 60
    assert wide.limit == 200

    narrow = LogsQuery(lookback_minutes=0, limit=0)
    assert narrow.lookback_minutes == 1
    assert narrow.limit == 1
