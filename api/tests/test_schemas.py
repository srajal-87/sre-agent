import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas import AlertmanagerWebhook

FIXTURE = Path(__file__).parent / "fixtures" / "alertmanager_firing.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parses_realistic_alertmanager_payload():
    webhook = AlertmanagerWebhook.model_validate(load_fixture())

    assert webhook.status == "firing"
    assert webhook.receiver == "sre-agent"
    assert webhook.groupKey.startswith("{}:{alertname=")
    assert webhook.commonLabels["service"] == "api-gateway"
    assert webhook.groupLabels["alertname"] == "HighErrorRate"
    assert len(webhook.alerts) == 1

    alert = webhook.alerts[0]
    assert alert.status == "firing"
    assert alert.labels["severity"] == "critical"
    assert alert.fingerprint == "a1b2c3d4e5f60718"
    assert alert.generatorURL.startswith("http://prometheus:9090")


def test_rejects_empty_alerts_list():
    payload = load_fixture()
    payload["alerts"] = []

    with pytest.raises(ValidationError):
        AlertmanagerWebhook.model_validate(payload)


def test_alert_optional_fields_default_to_empty():
    """Alertmanager omits annotations and endsAt on some payloads."""
    payload = load_fixture()
    del payload["alerts"][0]["annotations"]
    del payload["alerts"][0]["endsAt"]

    webhook = AlertmanagerWebhook.model_validate(payload)

    assert webhook.alerts[0].annotations == {}
    assert webhook.alerts[0].endsAt is None
