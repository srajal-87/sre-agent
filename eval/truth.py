"""Ground truth as the injector recorded it.

The injector appends one JSON object per injected fault to
``injector/ground_truth.jsonl``. This module is the only place that knows that
file's shape, so the scorer can work in typed objects.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, field_validator


# Where the injector appends. Repo-relative, so the harness and the tests read
# the same file without either of them knowing a working directory.
GROUND_TRUTH_PATH = Path(__file__).resolve().parent.parent / "injector" / "ground_truth.jsonl"

# The union of every victim service's SUPPORTED_FAULTS. Note this is four
# labels, while the agent's FaultType vocabulary is six - a truth label is what
# was injected, not what the agent is allowed to say.
TRUTH_FAULTS = {"timeout", "latency", "bad_config", "memory"}


def _parse_timestamp(value: str) -> datetime:
    """Parse either injector timestamp format into an aware UTC datetime.

    ``_utc_now_iso`` writes "...Z" with milliseconds; the correlated deploy's
    ``deployed_at`` comes from ``isoformat()`` and carries "+00:00" with
    microseconds. ``fromisoformat`` only learned "Z" in 3.11, so normalise it.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GroundTruth(BaseModel):
    """One injected fault: what it was, where, and when it ran."""

    incident_id: str
    fault: str
    target: str
    params: dict = {}
    started_at: datetime
    # Every optional field reads with a default: the earliest rows predate
    # correlated_deploy/deploy_error, and all rows written so far predate
    # truth_error. The file is append-only history, not a migrated table.
    ended_at: datetime | None = None
    correlated_deploy: dict | None = None
    deploy_error: str | None = None
    truth_error: str | None = None

    @field_validator("fault")
    @classmethod
    def _known_fault(cls, value: str) -> str:
        if value not in TRUTH_FAULTS:
            raise ValueError(
                f"unknown ground truth fault '{value}'; known: {sorted(TRUTH_FAULTS)}"
            )
        return value

    @field_validator("started_at", "ended_at", mode="before")
    @classmethod
    def _timestamps(cls, value):
        return _parse_timestamp(value) if isinstance(value, str) else value


def load_ground_truth(path: str | Path) -> list[GroundTruth]:
    """Read every row of a ground-truth JSONL into models."""
    rows: list[GroundTruth] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            # A killed injector leaves a half-written trailing line. Skip it -
            # but only for unparseable text: a well-formed row with a bad label
            # still raises, because that is a data error, not a torn write.
            continue
        rows.append(GroundTruth.model_validate(payload))
    return rows


def find_by_incident_id(
    rows: list[GroundTruth], incident_id: str
) -> GroundTruth | None:
    """The run -> truth join. Absent is an answer, not an error: the scorer
    voids a run whose truth row never landed rather than guessing at it."""
    for row in rows:
        if row.incident_id == incident_id:
            return row
    return None
