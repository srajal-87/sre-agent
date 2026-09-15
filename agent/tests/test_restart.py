"""restart_service, with Docker and the health poll injected. No socket needed."""

import asyncio
import json

from agent.tools.actions import ActionResult
from agent.tools.restart import RestartInput, RestartResult, restart_service


class _FakeContainer:
    def __init__(self, service: str, restart_error: Exception | None = None):
        self.labels = {
            "com.docker.compose.project": "sre-agent",
            "com.docker.compose.service": service,
        }
        self.name = f"sre-agent-{service}-1"
        self.status = "running"
        self.restart_calls = []
        self._restart_error = restart_error

    def restart(self, **kwargs):
        self.restart_calls.append(kwargs)
        if self._restart_error:
            raise self._restart_error


class _FakeDocker:
    def __init__(self, items):
        self._items = items
        self.list_calls = []

    @property
    def containers(self):
        return self

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        wanted = kwargs.get("filters", {}).get("label", [])
        return [
            c
            for c in self._items
            if all(
                label in {f"{k}={v}" for k, v in c.labels.items()} for label in wanted
            )
        ]


class _Health:
    """A scripted /health responder. Anything not an int is raised."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.urls = []

    async def __call__(self, url: str):
        self.urls.append(url)
        value = self._responses.pop(0) if self._responses else self._last()
        if isinstance(value, Exception):
            raise value
        return value

    def _last(self):
        return 200


SLEPT: list[float] = []


async def _no_sleep(seconds: float) -> None:
    SLEPT.append(seconds)


def _run(service="downstream-dep", *, client=None, health=None, **kwargs) -> RestartResult:
    SLEPT.clear()
    client = _FakeDocker([_FakeContainer(service)]) if client is None else client
    health = _Health(200) if health is None else health
    return asyncio.run(
        restart_service(
            RestartInput(service=service),
            docker_client=client,
            fetch=health,
            sleep=_no_sleep,
            **kwargs,
        )
    )


# ── happy path ───────────────────────────────────────────────────────

def test_the_container_is_restarted():
    container = _FakeContainer("downstream-dep")
    result = _run(client=_FakeDocker([container]))

    assert len(container.restart_calls) == 1
    assert result.ok is True
    assert result.executed is True
    assert result.dry_run is False
    assert result.target == "downstream-dep"
    assert result.tool == "restart_service"


def test_docker_is_given_a_stop_grace_period():
    """Without a timeout docker-py waits 10s by default; state it rather than inherit it."""
    container = _FakeContainer("downstream-dep")
    _run(client=_FakeDocker([container]))

    assert container.restart_calls[0]["timeout"] > 0


def test_the_container_is_resolved_by_compose_label():
    client = _FakeDocker([_FakeContainer("data-service")])
    _run("data-service", client=client)

    labels = client.list_calls[0]["filters"]["label"]
    assert "com.docker.compose.project=sre-agent" in labels
    assert "com.docker.compose.service=data-service" in labels


def test_the_query_is_the_literal_call_for_citation():
    result = _run("downstream-dep")
    assert result.query == "restart_service(service=downstream-dep)"


def test_the_source_names_docker_and_the_compose_project():
    result = _run()
    assert "docker" in result.source.lower()
    assert "sre-agent" in result.source


def test_latency_is_recorded():
    assert isinstance(_run().latency_ms, int)


# ── verification is independent of the action's own say-so ───────────

def test_a_healthy_service_is_verified_against_its_health_endpoint():
    health = _Health(200)
    result = _run(health=health)

    assert result.verification is not None
    assert "200" in result.verification
    assert health.urls[0].endswith("/health")
    assert "downstream-dep" in health.urls[0]


def test_the_poll_waits_out_a_service_that_is_still_coming_up():
    """A container that has just restarted refuses connections for a moment."""
    health = _Health(ConnectionError("connection refused"), 503, 200)
    result = _run(health=health)

    assert result.ok is True
    assert result.executed is True
    assert "200" in result.verification
    assert len(health.urls) == 3
    assert SLEPT  # it waited between attempts, without really waiting


def test_a_service_that_never_recovers_is_reported_honestly():
    """The restart ran; it just did not help. That is not the same as failing."""
    health = _Health(*([503] * 50))
    result = _run(health=health)

    assert result.executed is True  # the world did change
    assert result.ok is True  # the action itself did its job
    assert "503" in result.verification
    assert "not" in result.verification.lower()
    assert "did not" in result.summary.lower()


def test_an_unverifiable_restart_leaves_a_note():
    health = _Health(*([ConnectionError("refused")] * 50))
    result = _run(health=health)

    assert result.notes


# ── expected failures never raise ────────────────────────────────────

def test_an_unknown_service_is_refused_before_docker_is_touched():
    client = _FakeDocker([])
    result = asyncio.run(
        restart_service(
            RestartInput(service="postgres"), docker_client=client, fetch=_Health(200),
            sleep=_no_sleep,
        )
    )

    assert result.ok is False
    assert result.executed is False
    assert "postgres" in result.error
    assert "downstream-dep" in result.error
    assert client.list_calls == []


def test_a_missing_container_is_a_failure_not_an_empty_success():
    """'Empty is not failure' is a *read* rule; an action that cannot act failed."""
    result = _run(client=_FakeDocker([]))

    assert result.ok is False
    assert result.executed is False
    assert "downstream-dep" in result.error


def test_an_unavailable_socket_names_the_required_mount():
    class _Boom:
        @property
        def containers(self):
            raise RuntimeError("Error while fetching server API version")

    result = _run(client=_Boom())

    assert result.ok is False
    assert result.executed is False
    assert "/var/run/docker.sock" in result.error
    assert isinstance(result, RestartResult)


def test_a_restart_that_fails_is_reported_not_raised():
    container = _FakeContainer("downstream-dep", restart_error=RuntimeError("500 Server Error"))
    result = _run(client=_FakeDocker([container]))

    assert result.ok is False
    assert result.executed is False
    assert "500 Server Error" in result.error


# ── storable and console-safe ────────────────────────────────────────

def test_the_result_is_an_action_result():
    assert issubclass(RestartResult, ActionResult)


def test_the_result_survives_a_strict_json_dump():
    json.dumps(_run().model_dump(mode="json"), allow_nan=False)


def test_action_facing_strings_are_ascii():
    result = _run()
    result.summary.encode("ascii")
    result.verification.encode("ascii")
