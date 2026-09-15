"""The shared Docker access layer. No socket, no Docker SDK needed."""

import inspect

from agent.tools import containers, logs


class _FakeContainer:
    def __init__(self, service: str, project: str = "sre-agent", status: str = "running"):
        self.labels = {
            "com.docker.compose.project": project,
            "com.docker.compose.service": service,
        }
        self.name = f"{project}-{service}-1"
        self.status = status


class _FakeDocker:
    """Stands in for docker.DockerClient."""

    def __init__(self, items, list_error: Exception | None = None):
        self._items = items
        self._list_error = list_error
        self.list_calls = []

    @property
    def containers(self):
        return self

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if self._list_error:
            raise self._list_error
        wanted = kwargs.get("filters", {}).get("label", [])
        return [
            c
            for c in self._items
            if all(
                label in {f"{k}={v}" for k, v in c.labels.items()} for label in wanted
            )
        ]


def _stack(*services: str) -> _FakeDocker:
    return _FakeDocker([_FakeContainer(s) for s in services])


# ── find_container ───────────────────────────────────────────────────

def test_finds_the_container_for_a_service():
    client = _stack("api-gateway", "data-service", "downstream-dep")

    found = containers.find_container(client, "sre-agent", "data-service")

    assert found is not None
    assert found.labels[containers.SERVICE_LABEL] == "data-service"


def test_containers_are_resolved_by_compose_label_not_by_name():
    """Resolving by name breaks the moment the project is renamed."""
    client = _stack("data-service")

    containers.find_container(client, "sre-agent", "data-service")

    labels = client.list_calls[0]["filters"]["label"]
    assert "com.docker.compose.project=sre-agent" in labels
    assert "com.docker.compose.service=data-service" in labels


def test_a_stopped_container_is_still_found():
    """A crashed service is exactly the one you want to restart."""
    client = _FakeDocker([_FakeContainer("downstream-dep", status="exited")])

    found = containers.find_container(client, "sre-agent", "downstream-dep")

    assert found is not None
    assert client.list_calls[0]["all"] is True


def test_an_unknown_service_is_none_not_an_exception():
    found = containers.find_container(_stack("api-gateway"), "sre-agent", "nope")

    assert found is None


def test_another_projects_container_is_not_returned():
    client = _FakeDocker([_FakeContainer("data-service", project="someone-else")])

    assert containers.find_container(client, "sre-agent", "data-service") is None


def test_a_label_match_with_the_wrong_service_is_rejected():
    """The project label alone also matches prometheus and agent-api."""

    class _SloppyDocker(_FakeDocker):
        def list(self, **kwargs):  # ignores the filters entirely
            self.list_calls.append(kwargs)
            return self._items

    client = _SloppyDocker([_FakeContainer("prometheus")])

    assert client.list(filters={}) != []
    assert containers.find_container(client, "sre-agent", "data-service") is None


# ── connect ──────────────────────────────────────────────────────────

def test_the_docker_import_stays_inside_connect():
    """Load-bearing: it is what lets the unit suite run with no Docker SDK."""
    assert "import docker" in inspect.getsource(containers.connect)
    assert "import docker" not in inspect.getsource(containers).split("def connect")[0]


# ── no drift between the two callers ─────────────────────────────────

def test_logs_reads_its_labels_from_this_module():
    assert logs.PROJECT_LABEL is containers.PROJECT_LABEL
    assert logs.SERVICE_LABEL is containers.SERVICE_LABEL
