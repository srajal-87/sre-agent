"""Schema-level assertions on the ORM models. No database required."""

from app.models import Base, Incident, Investigation


def test_table_names():
    assert Incident.__tablename__ == "incidents"
    assert Investigation.__tablename__ == "investigations"
    assert set(Base.metadata.tables) == {"incidents", "investigations"}


def test_incident_columns_match_migration():
    columns = Base.metadata.tables["incidents"].columns
    assert set(columns.keys()) == {
        "id",
        "created_at",
        "source",
        "status",
        "receiver",
        "group_key",
        "service",
        "common_labels",
        "alert_count",
        "raw_payload",
        "ground_truth_fault",
        "ground_truth_target",
        "fault_started_at",
        "fault_ended_at",
    }
    assert columns["status"].nullable is False
    assert columns["alert_count"].nullable is False
    assert columns["raw_payload"].nullable is False
    # Ground truth is filled in later by the injector, so it must be nullable.
    assert columns["ground_truth_fault"].nullable is True
    assert columns["ground_truth_target"].nullable is True


def test_investigation_columns_match_migration():
    columns = Base.metadata.tables["investigations"].columns
    assert set(columns.keys()) == {
        "id",
        "incident_id",
        "created_at",
        "updated_at",
        "status",
        "diagnosis",
        "confidence",
        "action_taken",
        "action_result",
        "evidence",
        "steps",
        "cost_usd",
        "latency_ms",
        "langsmith_trace_id",
        "error",
    }
    assert columns["incident_id"].nullable is False
    assert columns["status"].nullable is False


def test_investigation_foreign_key_cascades():
    (fk,) = Base.metadata.tables["investigations"].c.incident_id.foreign_keys
    assert fk.column is Base.metadata.tables["incidents"].c.id
    assert fk.ondelete == "CASCADE"
