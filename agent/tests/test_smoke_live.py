"""Live smoke tests against a running stack. Opt-in.

    docker compose up -d
    SRE_AGENT_LIVE=1 PROMETHEUS_URL=http://localhost:9090 \
    DATABASE_URL=... e:/sre-agent/.venv/Scripts/python -m pytest tests/test_smoke_live.py

Asserts **invariants only** - never specific values - so a quiet stack, a
seeded-or-empty ledger, and an active fault all pass. Anything value-dependent
belongs in the offline suite, where the fixtures are pinned.
"""

import asyncio
import json
import os

import pytest

from agent.tools import TOOLS, run_tool
from agent.tools.base import ToolResult

pytestmark = pytest.mark.skipif(
    not os.getenv("SRE_AGENT_LIVE"), reason="SRE_AGENT_LIVE not set"
)

LIVE_CALLS = {
    "query_metrics": {"metric": "http_requests_total", "lookback_minutes": 15},
    "query_logs": {"lookback_minutes": 15, "limit": 5},
    "query_deploy_history": {"lookback_minutes": 1440},
}


def _call(name: str) -> ToolResult:
    return asyncio.run(run_tool(name, LIVE_CALLS[name]))


@pytest.mark.parametrize("name", sorted(LIVE_CALLS))
def test_each_tool_succeeds_against_the_real_backend(name):
    result = _call(name)
    assert result.ok is True, result.error


@pytest.mark.parametrize("name", sorted(LIVE_CALLS))
def test_each_result_is_self_describing(name):
    result = _call(name)

    assert result.tool == name
    assert result.summary.strip()
    assert result.source.strip()
    assert result.query.strip()  # the citation must never be empty on success
    assert result.error is None


@pytest.mark.parametrize("name", sorted(LIVE_CALLS))
def test_each_window_is_sane(name):
    result = _call(name)

    assert result.window is not None
    assert result.window.start < result.window.end
    assert result.window.duration_minutes > 0


@pytest.mark.parametrize("name", sorted(LIVE_CALLS))
def test_each_result_can_be_written_to_the_evidence_column(name):
    """The whole point of the envelope: model_dump -> jsonb, no adapter."""
    result = _call(name)
    encoded = json.dumps(result.model_dump(mode="json"), allow_nan=False)
    assert json.loads(encoded)["tool"] == name


@pytest.mark.parametrize("name", sorted(LIVE_CALLS))
def test_each_result_is_ascii_so_it_survives_any_console(name):
    result = _call(name)
    result.summary.encode("ascii")
    for note in result.notes:
        note.encode("ascii")


def test_metric_rows_parse_into_typed_points():
    result = _call("query_metrics")
    for series in result.series:
        assert isinstance(series.labels, dict)
        for point in series.points:
            assert point.ts.utcoffset().total_seconds() == 0
            assert isinstance(point.value, float)


def test_log_rows_carry_a_service_and_resolve_containers():
    result = _call("query_logs")

    # Container resolution must actually work; ok=True with zero lines is
    # otherwise indistinguishable from a broken compose-label lookup.
    assert not any("no container for" in note for note in result.notes), result.notes
    for line in result.lines:
        assert line.service


def test_deploy_rows_carry_a_computed_offset():
    result = _call("query_deploy_history")
    for record in result.deploys:
        assert isinstance(record.minutes_before_reference, float)
        assert record.deployed_at.utcoffset().total_seconds() == 0


def test_every_known_metric_is_queryable_against_the_live_prometheus():
    """Guards the catalogue against drift in the services' /metrics output."""
    from agent.tools.metrics import KNOWN_METRICS

    for metric, spec in KNOWN_METRICS.items():
        result = asyncio.run(
            run_tool("query_metrics", {"metric": metric, "service": spec["services"][0]})
        )
        assert result.ok is True, f"{metric}: {result.error}"


def test_the_registry_and_the_live_backends_agree_on_tool_names():
    for name in TOOLS:
        assert name in LIVE_CALLS, f"{name} has no live smoke coverage"
