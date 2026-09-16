"""Database access for incidents and investigations.

Deliberately thin: one method per thing the API does, no query-builder layer.
"""

import uuid

from sqlalchemy import select, update
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

    async def _update_investigation(
        self, investigation_id: uuid.UUID, values: dict
    ) -> bool:
        """Apply values to one investigation. False if there is no such row.

        The caller is a background task with nobody to raise at, so "wrote
        nothing" has to be a return value it can log rather than an exception
        that disappears into the event loop. ``updated_at`` looks after itself:
        the column carries onupdate=now().
        """
        async with self._sessionmaker() as session:
            async with session.begin():
                result = await session.execute(
                    update(Investigation)
                    .where(Investigation.id == investigation_id)
                    .values(**values)
                )
        return result.rowcount > 0

    async def mark_running(self, investigation_id: uuid.UUID) -> bool:
        """Move a pending stub to 'running', before the agent starts.

        Written before the run rather than after it so that a crash mid-
        investigation is distinguishable from one that was never picked up.
        """
        return await self._update_investigation(
            investigation_id, {"status": "running"}
        )

    async def complete_investigation(
        self, investigation_id: uuid.UUID, row: dict
    ) -> bool:
        """Fill in the finished investigation.

        ``row`` comes from ``app.records.investigation_row`` - already column
        names, already json-safe - so this method stays a write and holds no
        opinion about the report's shape.
        """
        return await self._update_investigation(investigation_id, row)

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
