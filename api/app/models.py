"""ORM models mirroring supabase/migrations/0001_incidents_investigations.sql.

The migration is the source of truth; these models must be kept in step with it
(``tests/test_models.py`` is the drift alarm).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime


class Base(DeclarativeBase):
    pass


class Incident(Base):
    """One Alertmanager webhook delivery, plus nullable injector ground truth."""

    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(
            "status in ('firing', 'resolved')", name="incidents_status_check"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    source: Mapped[str] = mapped_column(
        String, nullable=False, default="alertmanager", server_default="alertmanager"
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    receiver: Mapped[str | None] = mapped_column(String)
    group_key: Mapped[str | None] = mapped_column(String, index=True)
    service: Mapped[str | None] = mapped_column(String, index=True)
    common_labels: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    alert_count: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    ground_truth_fault: Mapped[str | None] = mapped_column(String)
    ground_truth_target: Mapped[str | None] = mapped_column(String)
    fault_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fault_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Investigation(Base):
    """One agent run against an incident. Created as a 'pending' stub."""

    __tablename__ = "investigations"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending', 'running', 'completed', 'failed')",
            name="investigations_status_check",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    diagnosis: Mapped[str | None] = mapped_column(String)
    confidence: Mapped[float | None] = mapped_column(Numeric)
    action_taken: Mapped[str | None] = mapped_column(String)
    action_result: Mapped[str | None] = mapped_column(String)
    evidence: Mapped[dict | list | None] = mapped_column(JSONB)
    steps: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    langsmith_trace_id: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
