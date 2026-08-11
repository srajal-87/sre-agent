from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from app.db import dispose_engine, get_sessionmaker
from app.logging import get_logger
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


@app.post("/investigate", response_model=InvestigateResponse, status_code=201)
async def investigate(
    webhook: AlertmanagerWebhook,
    repository: IncidentRepository = Depends(get_repository),
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
