"""Turning a webhook and a pile of ToolResults into what the model actually sees.

Two jobs here, and the second is where the token budget is won or lost.

``summarise_alert`` throws away the routing metadata: the model needs six
fields, not groupKey and generatorURL.

``render_result`` is summary-first. Every entry always contributes tool, ok,
summary, query, window and notes; the *rows* - metric points, log lines, deploy
records - are included only for the most recent iteration. This works unusually
well in this stack because the 3.1 tool summaries were written for an LLM to
reason on, so they carry most of the signal at a fraction of the tokens.
"""

import json
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel

from agent.graph.render import alert_brief, render_result, summarise_alert
from agent.graph.state import AlertSummary
from agent.tools.base import TimeWindow, ToolResult
from agent.tools.deploys import DeployResult
from agent.tools.metrics import MetricPoint, MetricSeries, MetricsResult

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _webhook(**overrides) -> dict:
    payload = {
        "version": "4",
        "groupKey": '{}:{alertname="GatewayTimeouts"}',
        "status": "firing",
        "receiver": "sre-agent",
        "groupLabels": {"alertname": "GatewayTimeouts"},
        "commonLabels": {
            "alertname": "GatewayTimeouts",
            "service": "api-gateway",
            "severity": "critical",
            "job": "api-gateway",
        },
        "commonAnnotations": {
            "description": "upstream_timeouts_total rate above threshold for 2m",
        },
        "externalURL": "http://alertmanager:9093",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "GatewayTimeouts",
                    "service": "api-gateway",
                    "severity": "critical",
                    "instance": "api-gateway:8000",
                },
                "annotations": {"summary": "api-gateway is returning 504s"},
                "startsAt": "2026-08-25T11:58:00.000Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus:9090/graph",
                "fingerprint": "a1b2c3d4e5f60718",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _metrics_result(**overrides) -> MetricsResult:
    fields = {
        "tool": "query_metrics",
        "summary": "p99 http_request_duration_seconds for api-gateway rose to 2.01",
        "source": "prometheus @ http://prometheus:9090/api/v1/query_range",
        "query": "histogram_quantile(0.99, sum by (le, instance) (rate(x[1m])))",
        "window": TimeWindow(start=T0, end=T0),
        "series": [
            MetricSeries(
                labels={"instance": "api-gateway:8000"},
                points=[MetricPoint(ts=T0, value=2.01)],
                latest=2.01,
            )
        ],
        "series_count": 1,
    }
    fields.update(overrides)
    return MetricsResult(**fields)


# -- distilling the webhook -------------------------------------------

def test_summarise_alert_keeps_the_six_fields_the_model_needs():
    alert = summarise_alert(_webhook())

    assert alert.alertname == "GatewayTimeouts"
    assert alert.service == "api-gateway"
    assert alert.severity == "critical"
    assert alert.summary == "api-gateway is returning 504s"
    assert "above threshold" in alert.description
    assert alert.started_at == datetime(2026, 8, 25, 11, 58, tzinfo=timezone.utc)


def test_summarise_alert_falls_back_to_the_common_labels():
    """Alertmanager puts shared labels on the group, not on each alert."""
    payload = _webhook()
    payload["alerts"][0]["labels"] = {}
    payload["alerts"][0]["annotations"] = {}

    alert = summarise_alert(payload)

    assert alert.alertname == "GatewayTimeouts"
    assert alert.service == "api-gateway"
    assert alert.severity == "critical"


def test_summarise_alert_prefers_a_firing_alert_over_a_resolved_one():
    payload = _webhook()
    resolved = json.loads(json.dumps(payload["alerts"][0]))
    resolved["status"] = "resolved"
    resolved["labels"]["service"] = "downstream-dep"
    payload["alerts"] = [resolved, payload["alerts"][0]]

    assert summarise_alert(payload).service == "api-gateway"


def test_summarise_alert_reads_the_service_off_the_instance_label():
    """Not every rule sets a service label; instance is "<service>:8000"."""
    payload = _webhook()
    payload["commonLabels"].pop("service")
    payload["alerts"][0]["labels"].pop("service")

    assert summarise_alert(payload).service == "api-gateway"


def test_summarise_alert_treats_the_zero_sentinel_start_as_no_start_time():
    """Alertmanager writes year 0001 for "unset"; using it would anchor every
    window to the first century."""
    payload = _webhook()
    payload["alerts"][0]["startsAt"] = "0001-01-01T00:00:00Z"

    assert summarise_alert(payload).started_at is None


def test_summarise_alert_accepts_a_pydantic_webhook_as_well_as_a_dict():
    """The API layer holds an AlertmanagerWebhook; the CLI holds parsed JSON."""

    class Alert(BaseModel):
        status: str = "firing"
        labels: dict = {}
        annotations: dict = {}
        startsAt: datetime | None = None

    class Webhook(BaseModel):
        status: str = "firing"
        commonLabels: dict = {}
        commonAnnotations: dict = {}
        alerts: list[Alert] = []

    model = Webhook(
        commonLabels={"alertname": "Boom", "service": "data-service"},
        alerts=[Alert(startsAt=T0)],
    )

    alert = summarise_alert(model)

    assert alert.alertname == "Boom"
    assert alert.started_at == T0


def test_summarise_alert_rejects_a_payload_with_no_alerts():
    """A pure helper raises; the caller turns it into a readable failure."""
    with pytest.raises(ValueError):
        summarise_alert(_webhook(alerts=[]))


# -- the alert brief --------------------------------------------------

def test_the_brief_carries_every_per_run_value():
    """These are exactly the values banned from the system prompt, so if they
    are not here the model never learns them."""
    alert = summarise_alert(_webhook())

    brief = alert_brief(alert, reference_time=T0)

    assert "GatewayTimeouts" in brief
    assert "api-gateway" in brief
    assert "critical" in brief
    assert "504s" in brief
    assert "above threshold" in brief
    assert T0.isoformat() in brief


def test_the_brief_is_ascii():
    brief = alert_brief(summarise_alert(_webhook()), reference_time=T0)

    brief.encode("ascii")


def test_the_brief_says_when_the_reference_time_is_a_fallback():
    """An alert with no start time is anchored to now, and the model should know
    the window it is reasoning about was chosen rather than reported."""
    alert = AlertSummary(alertname="Boom", service="data-service")

    brief = alert_brief(alert, reference_time=T0)

    assert T0.isoformat() in brief
    assert "no start time" in brief.lower()


def test_the_brief_survives_an_alert_with_no_annotations():
    alert = AlertSummary(alertname="Boom")

    brief = alert_brief(alert, reference_time=T0)

    assert "Boom" in brief


# -- rendering one result ---------------------------------------------

def test_a_summary_line_always_carries_the_citation_and_the_window():
    """query is the citation; without it in the collapsed form the model cannot
    cite evidence from an earlier turn."""
    rendered = render_result(_metrics_result(), detailed=False)

    assert "query_metrics" in rendered
    assert "rose to 2.01" in rendered
    assert "histogram_quantile(0.99" in rendered
    assert T0.isoformat() in rendered


def test_the_collapsed_form_drops_the_rows():
    rendered = render_result(_metrics_result(), detailed=False)

    assert "series" not in rendered


def test_the_detailed_form_carries_the_rows():
    rendered = render_result(_metrics_result(), detailed=True)

    assert "2.01" in rendered
    assert "series" in rendered


def test_the_detailed_form_does_not_repeat_the_envelope_in_its_json():
    """Duplicating summary/query/window inside the details doubles their cost."""
    rendered = render_result(_metrics_result(), detailed=True)
    details = rendered.split("details:", 1)[1]

    assert "summary" not in details
    assert "histogram_quantile" not in details


def test_the_detailed_json_is_compact():
    """json.dumps' default separators spend a token per key on whitespace."""
    rendered = render_result(_metrics_result(), detailed=True)

    assert ",\n" not in rendered.split("details:", 1)[1]


def test_a_failed_result_renders_its_error_and_stays_evidence():
    result = ToolResult(
        tool="query_logs",
        ok=False,
        summary="Could not reach the Docker socket.",
        source="docker",
        query="",
        error="permission denied",
    )

    rendered = render_result(result, detailed=True)

    assert "ok=false" in rendered.lower()
    assert "permission denied" in rendered


def test_notes_reach_the_model_because_they_qualify_the_evidence():
    result = _metrics_result(
        notes=["dropped 2 NaN/Inf point(s) - typically an empty histogram bucket."]
    )

    assert "NaN" in render_result(result, detailed=False)


def test_a_result_with_no_window_renders_without_one():
    result = DeployResult(
        tool="query_deploy_history",
        summary="No deploys in the 120m before 12:00:00Z",
        source="postgres deploys table",
        query="select * from deploys where ...",
    )

    rendered = render_result(result, detailed=False)

    assert "No deploys" in rendered
    assert "window:" not in rendered


def test_an_empty_result_reads_as_an_answer_not_as_a_failure():
    """ok=true with zero rows is a first-class observation; the rendering must
    not make it look like something went wrong."""
    result = DeployResult(
        tool="query_deploy_history",
        summary="No deploys in the 120m before 12:00:00Z, so a code change can "
        "be excluded as the trigger.",
        source="postgres deploys table",
        query="select * from deploys where ...",
        deploys=[],
    )

    rendered = render_result(result, detailed=True)

    assert "ok=true" in rendered.lower()
    assert "excluded" in rendered
