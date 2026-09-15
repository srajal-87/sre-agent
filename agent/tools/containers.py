"""Docker access shared by every tool that touches a container.

``query_logs`` reads container stdout; the write tools restart containers. Both
need the same two things — a client, and a way to turn a compose service name
into a container — so both live here rather than in whichever module needed
them first.

Containers are resolved by **compose label**, never by container name: the name
is ``{project}-{service}-{index}``, which changes the moment the project is
renamed or a service is scaled.
"""

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


def connect():
    """Build a Docker client from the environment.

    The ``docker`` import is deliberately **inside** the function so that
    importing any tool works on a machine with no Docker SDK and no socket —
    the same "imports never fail" rule that lets api/app/config.py default
    DATABASE_URL to "".
    """
    import docker

    return docker.from_env()


def find_container(client, project: str, service: str):
    """Return the container running ``service`` in ``project``, or None.

    Synchronous — docker-py is — so call it via ``asyncio.to_thread``.

    ``all=True`` includes stopped containers on purpose: a service that has
    exited is evidence when reading logs, and is exactly the one worth
    restarting. The service label is re-checked on the result because the
    project label alone also matches prometheus and agent-api.
    """
    containers = client.containers.list(
        all=True,
        filters={"label": [f"{PROJECT_LABEL}={project}", f"{SERVICE_LABEL}={service}"]},
    )
    for container in containers:
        if container.labels.get(SERVICE_LABEL) == service:
            return container
    return None
