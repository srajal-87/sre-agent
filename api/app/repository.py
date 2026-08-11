"""Database access for incidents and investigations.

Deliberately thin: one method per thing the API does, no query-builder layer.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Incident, Investigation
from app.schemas import AlertmanagerWebhook


def _service_of(webhook: AlertmanagerWebhook) -> str | None:
    """Best-effort service name: commonLabels wins, then groupLabels."""
    return webhook.commonLabels.get("service") or webhook.groupLabels.get("service")


class IncidentRepository:
    def __init__(self, sessionmaker: async_sessionmaker):
        self._sessionmaker = sessionmaker

    async def create_incident(
        self, webhook: AlertmanagerWebhook
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """Record the alert and open a 'pending' investigation for it.

        Both rows are written in a single transaction, so an incident never
        exists without an investigation to fill in later.
        """
        incident = Incident(
            status=webhook.status,
            receiver=webhook.receiver,
            group_key=webhook.groupKey,
            service=_service_of(webhook),
            common_labels=webhook.commonLabels,
            alert_count=len(webhook.alerts),
            raw_payload=webhook.model_dump(mode="json"),
        )
        async with self._sessionmaker() as session:
            async with session.begin():
                session.add(incident)
                # Flush to populate incident.id before the FK references it.
                await session.flush()
                investigation = Investigation(
                    incident_id=incident.id, status="pending"
                )
                session.add(investigation)

        return incident.id, investigation.id

    async def get_investigation(self, investigation_id: uuid.UUID) -> dict | None:
        async with self._sessionmaker() as session:
            investigation = await session.scalar(
                select(Investigation).where(Investigation.id == investigation_id)
            )
            if investigation is None:
                return None
            return {
                column.name: getattr(investigation, column.name)
                for column in Investigation.__table__.columns
            }
