"""Request/response models for the agent API.

Field names deliberately mirror the Alertmanager wire format (camelCase:
``groupKey``, ``commonLabels``, ``startsAt``, ``generatorURL``) so no aliasing
or ``populate_by_name`` config is needed to parse a webhook body verbatim.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class Alert(BaseModel):
    """A single alert inside an Alertmanager webhook."""

    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime | None = None
    endsAt: datetime | None = None
    generatorURL: str | None = None
    fingerprint: str | None = None


class AlertmanagerWebhook(BaseModel):
    """The body Alertmanager POSTs to a webhook receiver."""

    version: str | None = None
    groupKey: str | None = None
    truncatedAlerts: int = 0
    status: str
    receiver: str | None = None
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str | None = None
    alerts: list[Alert] = Field(min_length=1)


class InvestigateResponse(BaseModel):
    """Returned by POST /investigate once the incident has been recorded."""

    incident_id: UUID
    investigation_id: UUID
    status: str


class InvestigationResponse(BaseModel):
    """One row of the ``investigations`` table."""

    id: UUID
    incident_id: UUID
    created_at: datetime
    updated_at: datetime
    status: str
    diagnosis: str | None = None
    confidence: float | None = None
    action_taken: str | None = None
    action_result: str | None = None
    evidence: dict | list | None = None
    steps: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    langsmith_trace_id: str | None = None
    error: str | None = None
