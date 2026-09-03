"""Turning an alert and a pile of ToolResults into text the model reads.

Two jobs, and the second is where the token budget is won or lost.

``summarise_alert`` distils an Alertmanager webhook down to the six fields that
matter. It takes a dict or any object with ``model_dump`` rather than importing
``AlertmanagerWebhook``: ``api/`` is not an importable package, and the agent has
to keep working from the CLI, where ``api/app`` is not on the path.

``render_result`` is **summary-first**. Every entry always contributes tool, ok,
summary, query, window and notes; the rows - metric points, log lines, deploy
records - are included only for the most recent iteration, and older entries
collapse to those few lines. That works unusually well here because the 3.1 tool
summaries were written for an LLM to reason on, so they carry most of the signal
at a fraction of the tokens. The ``query`` survives collapsing because it is the
citation: without it the model cannot cite evidence from an earlier turn.
"""

import json
from datetime import datetime, timezone

from agent.graph.state import AlertSummary
from agent.tools.base import ToolResult

# Alertmanager writes year 0001 for an unset timestamp. Anchoring an
# investigation's windows to the first century would be a silent disaster, so
# anything implausibly old is read as "not reported".
_EPOCH_YEAR = 1970


def _as_dict(value) -> dict:
    """Accept a parsed webhook or a Pydantic model of one."""
    dump = getattr(value, "model_dump", None)
    return dump() if callable(dump) else value


def _parse_time(value) -> datetime | None:
    """Parse an Alertmanager timestamp, or return None if it says nothing."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return None if value.year < _EPOCH_YEAR else value


def _service(labels: dict) -> str | None:
    """The service the alert is about, however the rule happened to label it.

    ``instance`` is "<service>:8000" in this stack (infra/prometheus.yml), so it
    is a usable last resort when a rule sets neither service nor job.
    """
    for key in ("service", "job"):
        if labels.get(key):
            return labels[key]
    instance = labels.get("instance")
    return instance.split(":")[0] if instance else None


def summarise_alert(webhook) -> AlertSummary:
    """Distil a webhook into the six fields the model needs.

    Raises for a payload with no alerts: this is a pure helper, and the caller
    (the API route or the CLI) turns that into a readable failure.
    """
    payload = _as_dict(webhook)
    alerts = [_as_dict(alert) for alert in payload.get("alerts") or []]
    if not alerts:
        raise ValueError("webhook carries no alerts; there is nothing to investigate")

    # A group can arrive holding both firing and resolved alerts; the firing one
    # is the incident.
    alert = next((a for a in alerts if a.get("status") == "firing"), alerts[0])

    # Most specific wins: Alertmanager hoists shared labels onto the group.
    labels = {
        **(payload.get("groupLabels") or {}),
        **(payload.get("commonLabels") or {}),
        **(alert.get("labels") or {}),
    }
    annotations = {
        **(payload.get("commonAnnotations") or {}),
        **(alert.get("annotations") or {}),
    }

    return AlertSummary(
        alertname=labels.get("alertname") or "unknown",
        service=_service(labels),
        severity=labels.get("severity") or "unknown",
        summary=annotations.get("summary") or "",
        description=annotations.get("description") or "",
        started_at=_parse_time(alert.get("startsAt")),
    )


def alert_brief(alert: AlertSummary, *, reference_time: datetime) -> str:
    """The first user message: everything that is specific to this run.

    Every value here is banned from the system prompt, because a per-run string
    in the cached prefix invalidates the cache on every call. So this is the
    only place the model learns what it is investigating.
    """
    lines = [
        "INCIDENT",
        "",
        f"alert:       {alert.alertname}",
        f"severity:    {alert.severity}",
    ]
    if alert.service:
        lines.append(
            f"service:     {alert.service} (where the symptom was observed - the "
            f"fault may be elsewhere in the chain)"
        )
    if alert.summary:
        lines.append(f"summary:     {alert.summary}")
    if alert.description:
        lines.append(f"description: {alert.description}")

    lines.append("")
    if alert.started_at:
        lines.append(f"The alert started firing at {alert.started_at.isoformat()}.")
    else:
        lines.append(
            "The alert reported no start time, so the reference time below is "
            "when this investigation began rather than when the incident did."
        )
    lines.append(
        f"Reference time for this investigation: {reference_time.isoformat()}. "
        f"Every window you ask for is anchored to it, so a lookback of 30 minutes "
        f"means the 30 minutes before that moment."
    )
    return "\n".join(lines)


# The envelope fields, subtracted from a dump to leave each tool's own rows.
# Derived rather than listed so a new field on ToolResult cannot be rendered
# twice - once as a line and once inside the details JSON.
_ENVELOPE_FIELDS = set(ToolResult.model_fields)


def render_result(result: ToolResult, *, detailed: bool) -> str:
    """Render one tool result as the body of a tool_result block.

    ``detailed=False`` keeps the header lines only. Note that ``ok=false`` is
    rendered as an ordinary observation with an error line, not as a failure of
    the investigation: a broken tool is not a broken system.
    """
    lines = [
        f"[{result.tool}] ok={'true' if result.ok else 'false'}",
        f"summary: {result.summary}",
        f"source: {result.source}",
    ]
    if result.query:
        lines.append(f"query: {result.query}")
    if result.window:
        lines.append(
            f"window: {result.window.start.isoformat()} .. "
            f"{result.window.end.isoformat()}"
        )
    if result.notes:
        lines.append("notes: " + "; ".join(result.notes))
    if result.truncated:
        lines.append("truncated: true (some rows were not returned)")
    if result.error:
        lines.append(f"error: {result.error}")

    if detailed:
        payload = result.model_dump(mode="json")
        rows = {k: v for k, v in payload.items() if k not in _ENVELOPE_FIELDS}
        if rows:
            # Compact separators: json.dumps' defaults spend a token per key on
            # whitespace, and this is the largest thing in the message list.
            lines.append(
                "details: " + json.dumps(rows, separators=(",", ":"), default=str)
            )

    return "\n".join(lines)
