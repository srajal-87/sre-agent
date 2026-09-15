"""toggle_config, with the HTTP call injected. Nothing is reached."""

import asyncio
import json

import httpx

from agent.tools.actions import ActionResult
from agent.tools.toggle import ToggleInput, ToggleResult, toggle_config


class _Admin:
    """A scripted /admin/fault responder. An Exception value is raised."""

    def __init__(self, response=(200, '{"cleared":true}')):
        self._response = response
        self.calls = []

    async def __call__(self, url: str):
        self.calls.append(url)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _run(service="data-service", admin=None) -> ToggleResult:
    admin = _Admin() if admin is None else admin
    return asyncio.run(toggle_config(ToggleInput(service=service), delete=admin))


# ── happy path ───────────────────────────────────────────────────────

def test_the_admin_fault_surface_is_cleared():
    admin = _Admin()
    result = _run("data-service", admin)

    assert admin.calls == ["http://data-service:8000/admin/fault"]
    assert result.ok is True
    assert result.executed is True
    assert result.dry_run is False
    assert result.target == "data-service"
    assert result.tool == "toggle_config"


def test_the_query_is_the_literal_call_for_citation():
    assert _run("data-service").query == "toggle_config(service=data-service)"


def test_the_source_names_the_endpoint_that_was_called():
    result = _run("data-service")
    assert "/admin/fault" in result.source
    assert "data-service" in result.source


def test_the_verification_is_the_targets_own_answer():
    """Self-reported on purpose - the tool says so rather than implying more."""
    result = _run("data-service", _Admin((200, '{"cleared":true}')))

    assert "200" in result.verification
    assert "cleared" in result.verification


def test_the_note_points_at_the_independent_check():
    """The service confirming itself is not evidence the fault is gone."""
    result = _run("data-service")
    assert any("query_metrics" in n for n in result.notes)


def test_latency_is_recorded():
    assert isinstance(_run().latency_ms, int)


def test_it_works_for_every_victim_service():
    for service in ("api-gateway", "data-service", "downstream-dep"):
        assert _run(service).executed is True


# ── expected failures never raise ────────────────────────────────────

def test_an_unknown_service_is_refused_before_anything_is_called():
    admin = _Admin()
    result = asyncio.run(toggle_config(ToggleInput(service="postgres"), delete=admin))

    assert result.ok is False
    assert result.executed is False
    assert "postgres" in result.error
    assert admin.calls == []


def test_an_unreachable_service_is_reported_not_raised():
    result = _run("data-service", _Admin(httpx.ConnectError("connection refused")))

    assert result.ok is False
    assert result.executed is False
    assert "data-service" in result.error
    assert isinstance(result, ToggleResult)


def test_a_timeout_is_reported_as_a_timeout():
    result = _run("data-service", _Admin(httpx.TimeoutException("timed out")))

    assert result.ok is False
    assert "timed out" in result.error.lower() or "timeout" in result.error.lower()


def test_a_non_200_is_not_treated_as_a_change():
    """404 means nothing was cleared; claiming executed would be a false record."""
    result = _run("data-service", _Admin((404, "Not Found")))

    assert result.ok is False
    assert result.executed is False
    assert "404" in result.error


def test_a_server_error_body_is_carried_into_the_error():
    result = _run("data-service", _Admin((500, "internal explosion")))

    assert result.ok is False
    assert "internal explosion" in result.error


def test_an_enormous_body_does_not_end_up_in_the_summary():
    """The summary is read by a model on every turn; it is not a dumping ground."""
    result = _run("data-service", _Admin((500, "x" * 10000)))

    assert len(result.summary) < 500


# ── storable and console-safe ────────────────────────────────────────

def test_the_result_is_an_action_result():
    assert issubclass(ToggleResult, ActionResult)


def test_the_result_survives_a_strict_json_dump():
    json.dumps(_run().model_dump(mode="json"), allow_nan=False)


def test_action_facing_strings_are_ascii():
    result = _run()
    result.summary.encode("ascii")
    result.verification.encode("ascii")
