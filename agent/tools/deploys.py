"""query_deploy_history - read the synthetic deploy ledger out of Postgres.

The ledger is written by injector/seed_deploys.py (uncorrelated noise) and by
injector/inject.py (the deploy correlated with a fault). It lives in Postgres
rather than a file because the Phase 5 eval harness has to correlate deploys
with incidents.ground_truth_*, which are in Postgres too.

Two kinds of "nothing" are reported differently, and the distinction matters:

* **No deploys in the window** is a strong *negative* signal - the agent can
  rule out a code change.
* **A ledger with no rows at all** means the tool has no data. Without
  ``ledger_is_empty`` the agent could not tell those apart, and would
  confidently exonerate a real cause.
"""

import re
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String, func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime

from agent import config
from agent.tools.base import TimeWindow, ToolResult, failure, utc_now

TOOL_NAME = "query_deploy_history"

LOOKBACK_MINUTES_RANGE = (1, 1440)
LIMIT_RANGE = (1, 100)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class Base(DeclarativeBase):
    pass


class Deploy(Base):
    """Mirrors supabase/migrations/0002_deploys.sql, which is the source of truth."""

    __tablename__ = "deploys"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service: Mapped[str] = mapped_column(String, nullable=False, index=True)
    version: Mapped[str] = mapped_column(String, nullable=False)
    commit_sha: Mapped[str] = mapped_column(String, nullable=False)
    author: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(String, nullable=False)
    changed_files: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="succeeded", server_default="succeeded"
    )
    rolled_back_from: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))


class DeployQuery(BaseModel):
    service: str | None = Field(
        default=None, description="Restrict to one service. Omit for all of them."
    )
    lookback_minutes: int = Field(default=120, description="1 to 1440; clamped.")
    since: datetime | None = Field(
        default=None, description="Absolute window start; overrides lookback_minutes."
    )
    until: datetime | None = Field(default=None, description="Absolute window end.")
    # The incident time. Deploys are correlated against *this*, not against now.
    reference_time: datetime | None = Field(
        default=None,
        description=(
            "The incident time. Each deploy's offset is measured from it, and it "
            "anchors the end of the search window. Defaults to now."
        ),
    )
    limit: int = Field(default=20, description="Most recent deploys, 1 to 100; clamped.")

    @field_validator("lookback_minutes")
    @classmethod
    def _clamp_lookback(cls, value: int) -> int:
        return _clamp(value, *LOOKBACK_MINUTES_RANGE)

    @field_validator("limit")
    @classmethod
    def _clamp_limit(cls, value: int) -> int:
        return _clamp(value, *LIMIT_RANGE)


class DeployRecord(BaseModel):
    id: uuid.UUID
    service: str
    version: str
    commit_sha: str
    author: str
    message: str
    changed_files: list[str] = Field(default_factory=list)
    deployed_at: datetime
    status: str
    # Negative when the deploy shipped *after* the reference time, which rules
    # it out as a cause. Clamping this to zero would destroy that signal.
    minutes_before_reference: float


class DeployResult(ToolResult):
    deploys: list[DeployRecord] = Field(default_factory=list)
    services_seen: list[str] = Field(default_factory=list)
    ledger_is_empty: bool = False


# ── persistence ──────────────────────────────────────────────────────

# Keyed by event loop, not process-wide. A pooled asyncpg connection belongs to
# the loop that opened it, so a single cached engine raises "Event loop is
# closed" on the second asyncio.run() — which is exactly how the probe CLI, the
# eval harness, and the live smoke tests call these tools. The long-lived API
# process has one loop and so keeps one entry.
_sessionmakers: dict[object, object] = {}


def get_sessionmaker():
    """Return the async session factory for the running event loop.

    Mirrors api/app/db.py rather than importing it: the agent package is also
    used from the CLI (``python -m agent.tools.probe``), where the API's ``app``
    package is not on the path.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    try:
        key = asyncio.get_running_loop()
    except RuntimeError:
        key = None  # called outside a loop; one shared entry is fine

    existing = _sessionmakers.get(key)
    if existing is not None:
        return existing

    if not config.DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and set the "
            "Supabase session-pooler DSN."
        )
    engine = create_async_engine(
        config.DATABASE_URL,
        pool_size=5,
        max_overflow=0,
        # Supabase's pooler drops idle connections; check before handing one out.
        pool_pre_ping=True,
    )
    _sessionmakers[key] = async_sessionmaker(engine, expire_on_commit=False)
    return _sessionmakers[key]


class DeployRepository:
    """Deliberately thin: one method per thing the tool asks for."""

    def __init__(self, sessionmaker):
        self._sessionmaker = sessionmaker

    async def list_deploys(
        self, *, since: datetime, until: datetime, service: str | None, limit: int
    ) -> list[Deploy]:
        statement = select(Deploy).where(
            Deploy.deployed_at >= since, Deploy.deployed_at <= until
        )
        if service:
            statement = statement.where(Deploy.service == service)
        statement = statement.order_by(Deploy.deployed_at.desc()).limit(limit)

        async with self._sessionmaker() as session:
            return list(await session.scalars(statement))

    async def count_all(self) -> int:
        """Rows in the whole ledger, ignoring the window."""
        async with self._sessionmaker() as session:
            return await session.scalar(select(func.count()).select_from(Deploy))


# ── the tool ─────────────────────────────────────────────────────────

# Connection errors routinely embed the whole DSN, and this string can end up in
# the evidence jsonb column and from there in a UI.
_PASSWORD_IN_DSN = re.compile(r"(://[^:@/\s]+):[^@/\s]*@")


def _redact(text: str) -> str:
    return _PASSWORD_IN_DSN.sub(r"\1:***@", text)


def _as_utc(value: datetime) -> datetime:
    """timestamptz normally arrives aware; a naive one must not shift the diff."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_window(
    q: DeployQuery, *, now: Callable[[], datetime] = utc_now
) -> TimeWindow:
    """Resolve the search window.

    An explicit ``reference_time`` anchors the window's end: investigating an
    incident from five hours ago and being handed deploys that shipped *after*
    it is not useful. Explicit ``since``/``until`` still win over both.
    """
    if q.since is not None:
        return TimeWindow(start=q.since, end=q.until or now())
    if q.reference_time is not None:
        return TimeWindow.lookback(
            q.lookback_minutes, now=lambda: _as_utc(q.reference_time)
        )
    return TimeWindow.lookback(q.lookback_minutes, now=now)


def _query_text(q: DeployQuery, window: TimeWindow) -> str:
    """The SQL this observation came from, as the citation."""
    service = f" and service = '{q.service}'" if q.service else ""
    return (
        f"select * from deploys where deployed_at >= "
        f"'{window.start.isoformat()}' and deployed_at <= "
        f"'{window.end.isoformat()}'{service} order by deployed_at desc "
        f"limit {q.limit}"
    )


def _to_record(row: Deploy, reference: datetime) -> DeployRecord:
    deployed_at = _as_utc(row.deployed_at)
    offset = (reference - deployed_at).total_seconds() / 60.0
    return DeployRecord(
        id=row.id,
        service=row.service,
        version=row.version,
        commit_sha=row.commit_sha,
        author=row.author,
        message=row.message,
        changed_files=list(row.changed_files or []),
        deployed_at=deployed_at,
        status=row.status,
        minutes_before_reference=round(offset, 1),
    )


def _offset_phrase(record: DeployRecord) -> str:
    """Render the offset in prose. "-281.1 min before" is a double negative."""
    offset = record.minutes_before_reference
    if offset < 0:
        return f"{abs(offset)} min AFTER, so it cannot be the cause"
    return f"{offset} min before"


def summarise_deploys(
    q: DeployQuery,
    records: list[DeployRecord],
    reference: datetime,
    *,
    ledger_is_empty: bool,
    ledger_total: int,
) -> str:
    """One line, written for the LLM to reason on."""
    when = reference.strftime("%H:%M:%SZ")

    if ledger_is_empty:
        return (
            "The deploy ledger contains no rows at all - it may not have been "
            "seeded, so deploy history cannot be used as evidence either way."
        )

    if not records:
        return (
            f"No deploys in the {q.lookback_minutes}m before {when} "
            f"(the ledger holds {ledger_total} deploy(s) overall), so a code "
            f"change can be excluded as the trigger."
        )

    lead = records[0]
    files = ", ".join(lead.changed_files) or "no files recorded"
    text = (
        f"{len(records)} deploy(s) in the {q.lookback_minutes}m before {when}. "
        f"Most recent: {lead.service} {lead.version} at "
        f"{lead.deployed_at.strftime('%H:%M:%SZ')} "
        f"({_offset_phrase(lead)}) by {lead.author} - "
        f'"{lead.message}", touching {files}.'
    )
    if len(records) > 1:
        second = records[1]
        text += (
            f" Also {second.service} {second.version} at "
            f"{second.deployed_at.strftime('%H:%M:%SZ')} "
            f"({_offset_phrase(second)})."
        )
    return text


async def query_deploy_history(
    q: DeployQuery,
    *,
    repository: DeployRepository | None = None,
    sessionmaker=None,
    now: Callable[[], datetime] = utc_now,
) -> DeployResult:
    """Read recent deploys, with each one's offset from the incident computed.

    ``minutes_before_reference`` is computed here rather than left to the model:
    "shipped 2.3 minutes before the alert" is what actually correlates, and
    date arithmetic in-context is a reliable source of errors.
    """
    started = time.perf_counter()
    window = resolve_window(q, now=now)
    reference = _as_utc(q.reference_time) if q.reference_time else window.end
    source = "postgres deploys table"
    query_text = _query_text(q, window)

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        repo = repository or DeployRepository(sessionmaker or get_sessionmaker())
        rows = await repo.list_deploys(
            since=window.start, until=window.end, service=q.service, limit=q.limit
        )
        # Only when the window is empty do we need to tell "nothing shipped"
        # apart from "the ledger was never seeded".
        ledger_total = len(rows) if rows else await repo.count_all()
    except Exception as exc:  # noqa: BLE001 — a tool reports, it does not raise
        return failure(
            tool=TOOL_NAME, source=source, query=query_text, window=window,
            error=_redact(str(exc)),
            summary=(
                "Could not read the deploy ledger, so deploy history is "
                "unavailable; rely on metrics and logs instead."
            ),
            model=DeployResult, latency_ms=_elapsed_ms(),
        )

    records = [_to_record(row, reference) for row in rows]
    ledger_is_empty = not rows and ledger_total == 0

    notes: list[str] = []
    if ledger_is_empty:
        notes.append(
            "deploy ledger is empty - it may not be seeded "
            "(run: python injector/seed_deploys.py --noise 5)"
        )
    truncated = len(rows) >= q.limit
    if truncated:
        notes.append(
            f"hit the limit of {q.limit}; older deploys in the window were not returned."
        )

    return DeployResult(
        tool=TOOL_NAME,
        summary=summarise_deploys(
            q, records, reference,
            ledger_is_empty=ledger_is_empty, ledger_total=ledger_total,
        ),
        source=source,
        query=query_text,
        window=window,
        truncated=truncated,
        notes=notes,
        latency_ms=_elapsed_ms(),
        deploys=records,
        services_seen=sorted({r.service for r in records}),
        ledger_is_empty=ledger_is_empty,
    )
