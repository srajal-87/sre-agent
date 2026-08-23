"""query_deploy_history, with the sessionmaker injected. No database."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

from agent import config
from agent.tools import deploys as deploys_module
from agent.tools.deploys import (
    Deploy,
    DeployQuery,
    DeployResult,
    query_deploy_history,
)

FIXED = datetime(2026, 8, 20, 14, 9, 30, tzinfo=timezone.utc)


def _clock() -> datetime:
    return FIXED


def _row(minutes_ago: float, service="downstream-dep", **kwargs) -> Deploy:
    defaults = dict(
        id=uuid.uuid4(),
        service=service,
        version="v2.3.0",
        commit_sha="e4f19bc",
        author="alex.chen",
        message="tune connection pool settings",
        changed_files=["app/config.py", "app/client.py"],
        deployed_at=FIXED - timedelta(minutes=minutes_ago),
        status="succeeded",
    )
    return Deploy(**{**defaults, **kwargs})


class _FakeRepo:
    """Stands in for DeployRepository."""

    def __init__(self, rows, total=None, error=None):
        self._rows = rows
        self._total = len(rows) if total is None else total
        self._error = error
        self.calls = []

    async def list_deploys(self, *, since, until, service, limit):
        self.calls.append(
            {"since": since, "until": until, "service": service, "limit": limit}
        )
        if self._error:
            raise self._error
        return self._rows

    async def count_all(self):
        if self._error:
            raise self._error
        return self._total


def _run(q: DeployQuery, repo) -> DeployResult:
    return asyncio.run(query_deploy_history(q, repository=repo, now=_clock))


# ── happy path ───────────────────────────────────────────────────────

def test_returns_the_rows_with_a_citable_query_and_window():
    result = _run(DeployQuery(lookback_minutes=120), _FakeRepo([_row(2.3)]))

    assert result.ok is True
    assert result.tool == "query_deploy_history"
    assert len(result.deploys) == 1
    assert "deploys" in result.query
    assert result.window.end == FIXED
    assert "postgres" in result.source


def test_minutes_before_reference_is_computed_not_left_to_the_llm():
    """Date arithmetic in-context is a reliable source of errors."""
    result = _run(DeployQuery(), _FakeRepo([_row(2.3), _row(97.9, "api-gateway")]))

    assert result.deploys[0].minutes_before_reference == 2.3
    assert result.deploys[1].minutes_before_reference == 97.9


def test_reference_time_defaults_to_the_window_end():
    result = _run(DeployQuery(lookback_minutes=60), _FakeRepo([_row(15)]))
    assert result.deploys[0].minutes_before_reference == 15.0


def test_an_explicit_reference_time_wins():
    """The incident time, not 'now', is what a deploy must be correlated against."""
    incident = FIXED - timedelta(minutes=30)
    result = _run(DeployQuery(reference_time=incident), _FakeRepo([_row(35)]))
    assert result.deploys[0].minutes_before_reference == 5.0


def test_a_deploy_after_the_reference_time_is_negative():
    """Shipped *after* the alert fired: it cannot be the cause."""
    incident = FIXED - timedelta(minutes=30)
    result = _run(DeployQuery(reference_time=incident), _FakeRepo([_row(20)]))
    assert result.deploys[0].minutes_before_reference == -10.0


def test_reference_time_anchors_the_window_end():
    """Investigating a 5h-old incident must not return deploys made since."""
    incident = FIXED - timedelta(hours=5)
    repo = _FakeRepo([])
    _run(DeployQuery(reference_time=incident, lookback_minutes=120), repo)

    call = repo.calls[0]
    assert call["until"] == incident
    assert call["since"] == incident - timedelta(minutes=120)


def test_explicit_since_still_wins_over_reference_time():
    since = FIXED - timedelta(hours=8)
    repo = _FakeRepo([])
    _run(DeployQuery(since=since, until=FIXED, reference_time=FIXED - timedelta(hours=5)), repo)

    assert repo.calls[0]["since"] == since
    assert repo.calls[0]["until"] == FIXED


def test_a_deploy_after_the_reference_reads_as_after_not_minus_before():
    """"-281.1 min before" is a double negative an LLM should never have to parse."""
    incident = FIXED - timedelta(minutes=30)
    result = _run(
        DeployQuery(since=FIXED - timedelta(hours=2), until=FIXED, reference_time=incident),
        _FakeRepo([_row(20)]),
    )

    assert result.deploys[0].minutes_before_reference == -10.0
    assert "min AFTER" in result.summary
    assert "-10.0 min before" not in result.summary


def test_services_seen_lists_every_service_in_the_window():
    result = _run(
        DeployQuery(),
        _FakeRepo([_row(2.3, "downstream-dep"), _row(97.9, "api-gateway")]),
    )
    assert result.services_seen == ["api-gateway", "downstream-dep"]


def test_naive_timestamps_from_the_driver_are_treated_as_utc():
    """timestamptz normally arrives aware; a naive one must not shift the diff."""
    row = _row(2.3)
    row.deployed_at = row.deployed_at.replace(tzinfo=None)
    result = _run(DeployQuery(), _FakeRepo([row]))
    assert result.deploys[0].minutes_before_reference == 2.3


def test_the_repository_is_asked_for_the_resolved_window():
    repo = _FakeRepo([])
    _run(DeployQuery(lookback_minutes=120, service="data-service", limit=5), repo)

    call = repo.calls[0]
    assert call["until"] == FIXED
    assert call["since"] == FIXED - timedelta(minutes=120)
    assert call["service"] == "data-service"
    assert call["limit"] == 5


def test_the_result_survives_a_strict_json_dump_for_the_evidence_column():
    result = _run(DeployQuery(), _FakeRepo([_row(2.3)]))
    json.dumps(result.model_dump(mode="json"), allow_nan=False)


# ── the two distinguishable kinds of "nothing" ───────────────────────

def test_no_deploys_in_the_window_is_a_strong_negative_signal():
    result = _run(DeployQuery(lookback_minutes=120), _FakeRepo([], total=42))

    assert result.ok is True
    assert result.deploys == []
    assert result.ledger_is_empty is False
    assert "no deploys" in result.summary.lower()


def test_an_entirely_empty_ledger_is_flagged_separately():
    """Otherwise the agent cannot tell 'nothing shipped' from 'no data'."""
    result = _run(DeployQuery(), _FakeRepo([], total=0))

    assert result.ok is True
    assert result.ledger_is_empty is True
    assert any("seed" in n.lower() for n in result.notes)
    assert "excluded" not in result.summary.lower()


# ── truncation ───────────────────────────────────────────────────────

def test_hitting_the_limit_is_flagged_as_truncated():
    rows = [_row(i) for i in range(1, 21)]
    result = _run(DeployQuery(limit=20), _FakeRepo(rows, total=100))
    assert result.truncated is True


def test_lookback_and_limit_are_clamped_not_rejected():
    wide = DeployQuery(lookback_minutes=99999, limit=9999)
    assert wide.lookback_minutes == 1440
    assert wide.limit == 100

    narrow = DeployQuery(lookback_minutes=0, limit=0)
    assert narrow.lookback_minutes == 1
    assert narrow.limit == 1


# ── expected failures never raise ────────────────────────────────────

def test_a_missing_database_url_is_reported_with_the_remediation_sentence():
    result = _run(
        DeployQuery(),
        _FakeRepo([], error=RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and set the "
            "Supabase session-pooler DSN."
        )),
    )

    assert result.ok is False
    assert ".env" in result.error
    assert isinstance(result, DeployResult)


def test_an_unreachable_database_never_leaks_the_password():
    secret = "sup3rs3cret"
    result = _run(
        DeployQuery(),
        _FakeRepo([], error=RuntimeError(
            f"connection failed: postgresql+asyncpg://postgres.abc:{secret}"
            "@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
        )),
    )

    assert result.ok is False
    assert secret not in result.error
    assert secret not in result.summary
    assert "pooler.supabase.com" in result.error  # the host still helps


# ── summary ──────────────────────────────────────────────────────────

def test_summary_leads_with_the_most_recent_deploy_and_its_offset():
    result = _run(
        DeployQuery(lookback_minutes=120),
        _FakeRepo([_row(2.3, "downstream-dep"), _row(97.9, "api-gateway")]),
    )
    text = result.summary

    assert "2 deploy" in text
    assert "downstream-dep" in text
    assert "v2.3.0" in text
    assert "2.3" in text
    assert "alex.chen" in text
    assert "\n" not in text


# ── the engine must not outlive its event loop ───────────────────────

def test_the_sessionmaker_is_not_shared_across_event_loops():
    """A pooled asyncpg connection dies with its loop: "Event loop is closed"."""
    saved_url = config.DATABASE_URL
    saved_cache = dict(deploys_module._sessionmakers)
    try:
        deploys_module._sessionmakers.clear()
        # Never connected to — create_async_engine does not dial on construction.
        config.DATABASE_URL = "postgresql+asyncpg://u:p@localhost:5432/db"

        async def _get():
            return deploys_module.get_sessionmaker()

        first = asyncio.run(_get())
        second = asyncio.run(_get())
        assert first is not second
    finally:
        config.DATABASE_URL = saved_url
        deploys_module._sessionmakers.clear()
        deploys_module._sessionmakers.update(saved_cache)


def test_the_sessionmaker_is_reused_within_one_event_loop():
    saved_url = config.DATABASE_URL
    saved_cache = dict(deploys_module._sessionmakers)
    try:
        deploys_module._sessionmakers.clear()
        config.DATABASE_URL = "postgresql+asyncpg://u:p@localhost:5432/db"

        async def _twice():
            return deploys_module.get_sessionmaker(), deploys_module.get_sessionmaker()

        first, second = asyncio.run(_twice())
        assert first is second
    finally:
        config.DATABASE_URL = saved_url
        deploys_module._sessionmakers.clear()
        deploys_module._sessionmakers.update(saved_cache)


def test_summaries_and_notes_are_ascii():
    result = _run(DeployQuery(), _FakeRepo([], total=0))
    result.summary.encode("ascii")
    for note in result.notes:
        note.encode("ascii")
