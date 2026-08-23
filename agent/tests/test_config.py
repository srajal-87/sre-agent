"""agent.config is import-safe with no environment set, and honours overrides."""

import importlib
import os
from types import SimpleNamespace

import agent.config

_CONSTANTS = ("PROMETHEUS_URL", "COMPOSE_PROJECT", "LOG_SERVICES", "DATABASE_URL")


def _reload(**env) -> SimpleNamespace:
    """Reload agent.config with the given env vars applied (others cleared).

    Returns a *snapshot* of the constants, not the module: the module is a
    singleton that the restoring reload below would otherwise mutate back to
    its defaults before the caller ever reads it.
    """
    saved = {k: os.environ.get(k) for k in ("PROMETHEUS_URL", "COMPOSE_PROJECT")}
    try:
        for key in saved:
            os.environ.pop(key, None)
        os.environ.update(env)
        reloaded = importlib.reload(agent.config)
        return SimpleNamespace(**{k: getattr(reloaded, k) for k in _CONSTANTS})
    finally:
        for key, value in saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        importlib.reload(agent.config)


def test_prometheus_url_defaults_to_the_compose_service():
    cfg = _reload()
    assert cfg.PROMETHEUS_URL == "http://prometheus:9090"


def test_prometheus_url_honours_the_env_override():
    cfg = _reload(PROMETHEUS_URL="http://localhost:9090")
    assert cfg.PROMETHEUS_URL == "http://localhost:9090"


def test_prometheus_url_has_no_trailing_slash():
    cfg = _reload(PROMETHEUS_URL="http://localhost:9090/")
    assert cfg.PROMETHEUS_URL == "http://localhost:9090"


def test_compose_project_defaults_to_the_repo_directory_name():
    cfg = _reload()
    assert cfg.COMPOSE_PROJECT == "sre-agent"


def test_log_services_are_the_three_victim_services():
    cfg = _reload()
    assert cfg.LOG_SERVICES == ["api-gateway", "data-service", "downstream-dep"]


def test_database_url_is_reused_from_the_environment_not_redefined():
    """The agent reuses DATABASE_URL; it must be import-safe when unset."""
    cfg = _reload()
    assert isinstance(cfg.DATABASE_URL, str)
