import json

from app.logging import get_logger


def test_log_line_is_json_with_required_fields(capsys):
    logger = get_logger("api-gateway")
    logger.info("hello world", extra={"trace_id": "abc-123"})

    out = capsys.readouterr().out.strip()
    record = json.loads(out)  # must be valid single-line JSON

    assert record["service"] == "api-gateway"
    assert record["level"] == "INFO"
    assert record["message"] == "hello world"
    assert record["trace_id"] == "abc-123"
    assert "timestamp" in record


def test_trace_id_defaults_to_none_when_absent(capsys):
    logger = get_logger("api-gateway")
    logger.warning("no trace here")

    record = json.loads(capsys.readouterr().out.strip())
    assert record["level"] == "WARNING"
    assert record["trace_id"] is None
