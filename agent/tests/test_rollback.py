"""rollback_deploy, with the repository injected. No database."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

from agent.tools.actions import ActionResult
from agent.tools.deploys import Deploy
from agent.tools.rollback import RollbackInput, RollbackResult, rollback_deploy

FIXED = datetime(2026, 8, 20, 14, 9, 30, tzinfo=timezone.utc)


def _clock() -> datetime:
    return FIXED


def _row(minutes_ago: float, service="data-service", **kwargs) -> Deploy:
    defaults = dict(
        id=uuid.uuid4(),
        service=service,
        version="v2.3.0",
        commit_sha="e4f19bc",
        author="alex.chen",
        message="refactor configuration loading",
        changed_files=["app/config.py"],
        deployed_at=FIXED - timedelta(minutes=minutes_ago),
        status="succeeded",
    )
    return Deploy(**{**defaults, **kwargs})


class _FakeRepo:
    """Stands in for RollbackRepository: newest-first history per service."""

    def __init__(self, rows, error=None):
        self._rows = rows
        self._error = error
        self.inserted = []

    async def history(self, service: str, limit: int) -> list[Deploy]:
        if self._error:
            raise self._error
        rows = [r for r in self._rows if r.service == service]
        return sorted(rows, key=lambda r: r.deployed_at, reverse=True)[:limit]

    async def insert(self, deploy: Deploy) -> None:
        if self._error:
            raise self._error
        self.inserted.append(deploy)


def _history(*, service="data-service"):
    """A current deploy and the one it replaced."""
    return [
        _row(30, service=service, version="v2.3.0", commit_sha="e4f19bc"),
        _row(600, service=service, version="v2.2.0", commit_sha="a1b2c3d"),
    ]


def _run(repo, service="data-service", deploy_id=None) -> RollbackResult:
    return asyncio.run(
        rollback_deploy(
            RollbackInput(service=service, deploy_id=deploy_id),
            repository=repo,
            now=_clock,
        )
    )


# ── happy path ───────────────────────────────────────────────────────

def test_a_ledger_row_is_written_for_the_rollback():
    repo = _FakeRepo(_history())
    result = _run(repo)

    assert len(repo.inserted) == 1
    assert result.ok is True
    assert result.executed is True
    assert result.target == "data-service"
    assert result.tool == "rollback_deploy"


def test_the_new_row_restores_the_previous_version():
    repo = _FakeRepo(_history())
    _run(repo)

    written = repo.inserted[0]
    assert written.service == "data-service"
    assert written.version == "v2.2.0"
    assert written.commit_sha == "a1b2c3d"


def test_the_new_row_points_back_at_what_it_reverted():
    """rolled_back_from is what makes this row distinguishable from a real deploy."""
    rows = _history()
    repo = _FakeRepo(rows)
    _run(repo)

    assert repo.inserted[0].rolled_back_from == rows[0].id


def test_the_agent_is_recorded_as_the_author():
    repo = _FakeRepo(_history())
    _run(repo)

    assert "agent" in repo.inserted[0].author
    assert "v2.3.0" in repo.inserted[0].message


def test_the_row_is_stamped_with_the_injected_clock():
    repo = _FakeRepo(_history())
    _run(repo)

    assert repo.inserted[0].deployed_at == FIXED


def test_a_named_deploy_id_is_rolled_back_rather_than_the_latest():
    rows = _history()
    older = rows[1]
    repo = _FakeRepo(rows + [_row(5, service="data-service", version="v2.4.0")])

    result = _run(repo, deploy_id=str(older.id))

    assert result.ok is False  # nothing older than it to restore
    assert "v2.2.0" in result.error


def test_the_query_is_the_literal_call_for_citation():
    result = _run(_FakeRepo(_history()))
    assert result.query.startswith("rollback_deploy(service=data-service")


def test_the_source_names_the_ledger():
    assert "deploys" in _run(_FakeRepo(_history())).source


def test_the_note_says_no_code_actually_moved():
    """Honesty: there is no deploy mechanism here, only a ledger."""
    result = _run(_FakeRepo(_history()))

    assert any("ledger" in n.lower() for n in result.notes)
    assert any("simulat" in n.lower() for n in result.notes)


def test_latency_is_recorded():
    assert isinstance(_run(_FakeRepo(_history())).latency_ms, int)


# ── expected failures never raise ────────────────────────────────────

def test_an_unknown_service_is_refused_before_the_ledger_is_read():
    repo = _FakeRepo(_history())
    result = _run(repo, service="postgres")

    assert result.ok is False
    assert result.executed is False
    assert "postgres" in result.error
    assert repo.inserted == []


def test_a_service_with_no_deploys_cannot_be_rolled_back():
    repo = _FakeRepo([])
    result = _run(repo)

    assert result.ok is False
    assert result.executed is False
    assert "data-service" in result.error
    assert repo.inserted == []


def test_a_single_deploy_has_nothing_to_roll_back_to():
    repo = _FakeRepo([_row(30, version="v1.0.0")])
    result = _run(repo)

    assert result.ok is False
    assert result.executed is False
    assert "v1.0.0" in result.error
    assert repo.inserted == []


def test_an_unknown_deploy_id_is_reported_not_raised():
    unknown = str(uuid.uuid4())
    repo = _FakeRepo(_history())
    result = _run(repo, deploy_id=unknown)

    assert result.ok is False
    assert unknown in result.error
    assert repo.inserted == []


def test_a_malformed_deploy_id_is_reported_not_raised():
    """The model will send 'latest' one day; that must not kill the graph."""
    repo = _FakeRepo(_history())
    result = _run(repo, deploy_id="latest")

    assert result.ok is False
    assert "latest" in result.error
    assert repo.inserted == []


def test_an_unreachable_database_is_reported_not_raised():
    repo = _FakeRepo(_history(), error=RuntimeError("connection refused"))
    result = _run(repo)

    assert result.ok is False
    assert result.executed is False
    assert "connection refused" in result.error
    assert isinstance(result, RollbackResult)


def test_a_dsn_password_never_reaches_the_error():
    """This string lands in the action_result column and from there in a UI."""
    boom = RuntimeError("could not connect to postgresql://user:hunter2@db:5432/x")
    result = _run(_FakeRepo(_history(), error=boom))

    assert "hunter2" not in result.error
    assert "***" in result.error


# ── storable and console-safe ────────────────────────────────────────

def test_the_result_is_an_action_result():
    assert issubclass(RollbackResult, ActionResult)


def test_the_result_survives_a_strict_json_dump():
    json.dumps(_run(_FakeRepo(_history())).model_dump(mode="json"), allow_nan=False)


def test_action_facing_strings_are_ascii():
    result = _run(_FakeRepo(_history()))
    result.summary.encode("ascii")
    result.verification.encode("ascii")
