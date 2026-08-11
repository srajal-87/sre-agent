from fastapi import Depends, FastAPI

from app.db import get_sessionmaker
from app.logging import get_logger
from app.repository import IncidentRepository
from app.schemas import AlertmanagerWebhook, InvestigateResponse

SERVICE_NAME = "agent-api"

app = FastAPI(title=SERVICE_NAME)
log = get_logger(SERVICE_NAME)


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


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}
