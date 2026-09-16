from contextlib import asynccontextmanager
from typing import Callable
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.responses import JSONResponse

from app import config
from app.db import dispose_engine, get_sessionmaker
from app.logging import get_logger
from app.records import investigation_row
from app.repository import IncidentRepository
from app.schemas import (
    AlertmanagerWebhook,
    InvestigateResponse,
    InvestigationResponse,
)

SERVICE_NAME = "agent-api"

log = get_logger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Open the connection pool at startup, close it cleanly at shutdown."""
    get_sessionmaker()
    log.info("database pool ready")
    yield
    await dispose_engine()


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)


def get_repository() -> IncidentRepository:
    """Dependency seam: tests override this with an in-memory fake."""
    return IncidentRepository(get_sessionmaker())


def get_investigator() -> Callable:
    """Dependency seam: what actually runs an investigation.

    Imported here rather than at module scope so that ``app.main`` still
    imports with no model client, no Docker socket and no Prometheus - the
    property that keeps the api unit tests offline. Tests override this with a
    fake and never reach the import at all.
    """
    from agent.graph import investigate as run_investigation

    return run_investigation


async def run_investigation(
    *,
    repository: IncidentRepository,
    investigator: Callable,
    webhook: AlertmanagerWebhook,
    incident_id: UUID,
    investigation_id: UUID,
) -> None:
    """Investigate one recorded incident and write the result to its row.

    Runs after the response has been sent, so it has nobody to raise at: it
    reports through the row instead. The alert is summarised here rather than in
    the endpoint because that is agent-shaped work, and the endpoint's job is to
    record the webhook and answer.
    """
    from agent.graph.render import summarise_alert

    try:
        await repository.mark_running(investigation_id)
        report = await investigator(
            summarise_alert(webhook),
            incident_id=incident_id,
            investigation_id=investigation_id,
        )
        await repository.complete_investigation(
            investigation_id, investigation_row(report)
        )
        log.info(
            "investigation complete",
            extra={
                "investigation_id": str(investigation_id),
                "status": report.status,
                "confidence": report.confidence,
                "cost_usd": report.cost_usd,
                "trace_id": str(report.trace_id) if report.trace_id else None,
            },
        )
    except Exception as exc:  # noqa: BLE001 - records, does not raise
        # The graph reports its own failures as a report with status="failed",
        # so reaching here means something outside it broke. Left to itself the
        # exception would vanish into the event loop and the row would read
        # 'running' for ever - which is indistinguishable from an investigation
        # still in progress.
        await _record_failure(repository, investigation_id, exc)


async def _record_failure(
    repository: IncidentRepository, investigation_id: UUID, exc: Exception
) -> None:
    """Write the failure to the row, and give up quietly if even that fails.

    The last write is as likely to fail as the run was - a dropped pooler
    connection, a deleted incident - and by then there is nowhere left to
    report it.
    """
    error = f"{type(exc).__name__}: {exc}"
    log.error(
        "investigation failed",
        extra={"investigation_id": str(investigation_id), "error": error},
    )
    try:
        await repository.complete_investigation(
            investigation_id, {"status": "failed", "error": error}
        )
    except Exception:  # noqa: BLE001 - nothing left to tell
        log.error(
            "could not record the failure",
            extra={"investigation_id": str(investigation_id)},
        )


@app.post("/investigate", response_model=InvestigateResponse, status_code=201)
async def investigate(
    webhook: AlertmanagerWebhook,
    background_tasks: BackgroundTasks,
    repository: IncidentRepository = Depends(get_repository),
    investigator: Callable = Depends(get_investigator),
):
    incident_id, investigation_id = await repository.create_incident(webhook)
    log.info(
        "incident recorded",
        extra={
            "incident_id": str(incident_id),
            "investigation_id": str(investigation_id),
            "alert_count": len(webhook.alerts),
        },
    )

    # 201 pending is still the answer. An investigation takes a minute or more,
    # and Alertmanager is not going to hold the connection open for it.
    if config.AGENT_AUTO_INVESTIGATE:
        background_tasks.add_task(
            run_investigation,
            repository=repository,
            investigator=investigator,
            webhook=webhook,
            incident_id=incident_id,
            investigation_id=investigation_id,
        )

    return InvestigateResponse(
        incident_id=incident_id,
        investigation_id=investigation_id,
        status="pending",
    )


@app.get("/investigations/{investigation_id}", response_model=InvestigationResponse)
async def get_investigation(
    investigation_id: UUID,
    repository: IncidentRepository = Depends(get_repository),
):
    investigation = await repository.get_investigation(investigation_id)
    if investigation is None:
        return JSONResponse(
            status_code=404, content={"error": "investigation not found"}
        )
    return investigation


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}
