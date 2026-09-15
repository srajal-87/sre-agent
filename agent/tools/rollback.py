"""rollback_deploy - record that a service was reverted to its previous version.

**Honestly simulated, and the result says so.** This stack has no deploy
mechanism: the "deploys" are rows written by injector/seed_deploys.py, and the
running code is whatever docker compose built. So a rollback here writes a new
ledger row restoring the previous version, with ``rolled_back_from`` pointing at
what it reverted, and nothing in the running system changes. Every result
carries a note saying exactly that, because an action that quietly implied more
than it did would poison the evidence trail it is meant to strengthen.

It earns its place anyway: it is classified high blast radius in the policy
table and is therefore always denied. A gate that never refuses demonstrates
nothing.
"""

import time
import uuid
from datetime import datetime
from typing import Callable

from pydantic import Field

from agent import config
from agent.tools.base import ActionInput, ActionResult, action_query, failure, utc_now
from agent.tools.deploys import Deploy, get_sessionmaker, redact

TOOL_NAME = "rollback_deploy"

AGENT_AUTHOR = "sre-agent"

# Deep enough to find a predecessor past a couple of prior rollbacks, shallow
# enough that the read stays trivial.
HISTORY_LIMIT = 20


class RollbackInput(ActionInput):
    """Which service to revert, and optionally which deploy to revert."""

    deploy_id: str | None = Field(
        default=None,
        description=(
            "The deploy to roll back, as its id from query_deploy_history. Omit "
            "to roll back the most recent deploy for the service."
        ),
    )


class RollbackResult(ActionResult):
    """No extra fields: the envelope says everything a ledger write did."""


class RollbackRepository:
    """Deliberately thin, like DeployRepository: one method per thing asked for."""

    def __init__(self, sessionmaker):
        self._sessionmaker = sessionmaker

    async def history(self, service: str, limit: int) -> list[Deploy]:
        """The service's deploys, newest first."""
        from sqlalchemy import select

        statement = (
            select(Deploy)
            .where(Deploy.service == service)
            .order_by(Deploy.deployed_at.desc())
            .limit(limit)
        )
        async with self._sessionmaker() as session:
            return list(await session.scalars(statement))

    async def insert(self, deploy: Deploy) -> None:
        async with self._sessionmaker() as session:
            session.add(deploy)
            await session.commit()


async def rollback_deploy(
    q: RollbackInput,
    *,
    repository: RollbackRepository | None = None,
    sessionmaker=None,
    now: Callable[[], datetime] = utc_now,
) -> RollbackResult:
    """Revert ``service`` to the version that preceded its current deploy."""
    started = time.perf_counter()
    source = "postgres deploys table"
    query = action_query(TOOL_NAME, q)

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    def _failed(error: str, summary: str) -> RollbackResult:
        return failure(
            tool=TOOL_NAME, source=source, query=query,
            error=redact(error), summary=summary,
            model=RollbackResult, latency_ms=_elapsed_ms(),
        )

    if q.service not in config.SERVICE_PORTS:
        return _failed(
            error=f"unknown service '{q.service}'; known: {sorted(config.SERVICE_PORTS)}",
            summary=(
                f"There is no service called '{q.service}' to roll back. Known: "
                f"{', '.join(sorted(config.SERVICE_PORTS))}."
            ),
        )

    target_id: uuid.UUID | None = None
    if q.deploy_id is not None:
        try:
            target_id = uuid.UUID(q.deploy_id)
        except ValueError:
            return _failed(
                error=f"'{q.deploy_id}' is not a deploy id; use an id from query_deploy_history",
                summary=(
                    f"'{q.deploy_id}' is not a deploy id. Omit deploy_id to roll "
                    f"back the most recent deploy, or take an id from "
                    f"query_deploy_history."
                ),
            )

    try:
        repo = repository or RollbackRepository(sessionmaker or get_sessionmaker())
        history = await repo.history(q.service, HISTORY_LIMIT)
    except Exception as exc:  # noqa: BLE001 - an action reports, it does not raise
        return _failed(
            error=f"could not read the deploy ledger: {exc}",
            summary=(
                "Could not read the deploy ledger, so nothing was rolled back."
            ),
        )

    if not history:
        return _failed(
            error=f"no deploys recorded for '{q.service}'",
            summary=(
                f"The ledger holds no deploys for '{q.service}', so there is "
                f"nothing to roll back."
            ),
        )

    if target_id is None:
        index = 0
    else:
        index = next(
            (i for i, row in enumerate(history) if row.id == target_id), -1
        )
        if index < 0:
            return _failed(
                error=(
                    f"deploy '{q.deploy_id}' is not among the last "
                    f"{HISTORY_LIMIT} deploys of '{q.service}'"
                ),
                summary=(
                    f"No such deploy '{q.deploy_id}' for '{q.service}'; nothing "
                    f"was rolled back."
                ),
            )

    target = history[index]
    if index + 1 >= len(history):
        return _failed(
            error=(
                f"'{target.version}' is the earliest recorded deploy of "
                f"'{q.service}'; there is no previous version to restore"
            ),
            summary=(
                f"'{q.service}' has no deploy earlier than {target.version}, so "
                f"there is no version to roll back to."
            ),
        )

    previous = history[index + 1]

    row = Deploy(
        id=uuid.uuid4(),
        service=q.service,
        version=previous.version,
        commit_sha=previous.commit_sha,
        author=AGENT_AUTHOR,
        message=f"rollback of {target.version} ({target.commit_sha}) to {previous.version}",
        # The files the revert touches are the ones the reverted deploy changed.
        changed_files=list(target.changed_files or []),
        deployed_at=now(),
        status="succeeded",
        rolled_back_from=target.id,
    )

    try:
        await repo.insert(row)
    except Exception as exc:  # noqa: BLE001 - same reporting rule
        return _failed(
            error=f"could not write the rollback to the ledger: {exc}",
            summary=f"The rollback of '{q.service}' was not recorded: {exc}",
        )

    verification = (
        f"ledger row {row.id} records '{q.service}' back at {previous.version}, "
        f"rolled_back_from={target.id}"
    )

    return RollbackResult(
        tool=TOOL_NAME,
        ok=True,
        summary=(
            f"Rolled '{q.service}' back from {target.version} to "
            f"{previous.version} ({verification})."
        ),
        source=source,
        query=query,
        target=q.service,
        executed=True,
        verification=verification,
        notes=[
            "Simulated: this stack has no deploy mechanism, so the rollback is "
            "recorded in the deploy ledger and the running code is unchanged."
        ],
        latency_ms=_elapsed_ms(),
    )
