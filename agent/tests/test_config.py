"""agent.config is import-safe with no environment set, and honours overrides."""

import importlib
import os
from types import SimpleNamespace

import agent.config

_CONSTANTS = (
    "PROMETHEUS_URL",
    "COMPOSE_PROJECT",
    "LOG_SERVICES",
    "DATABASE_URL",
    "AGENT_ALLOW_WRITES",
    "ACTION_TIMEOUT_SECONDS",
    "AUTO_ACTION_CONFIDENCE",
    "LANGSMITH_TRACING",
    "LANGSMITH_PROJECT",
)

# Env var names, not constants: LANGCHAIN_TRACING_V2 is read but not exported,
# and it has to be cleared or a developer's own shell would decide the default.
_MANAGED = ("PROMETHEUS_URL", "COMPOSE_PROJECT", "AGENT_ALLOW_WRITES",
            "ACTION_TIMEOUT_SECONDS", "AUTO_ACTION_CONFIDENCE",
            "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_PROJECT")


def _reload(**env) -> SimpleNamespace:
    """Reload agent.config with the given env vars applied (others cleared).

    Returns a *snapshot* of the constants, not the module: the module is a
    singleton that the restoring reload below would otherwise mutate back to
    its defaults before the caller ever reads it.
    """
    saved = {k: os.environ.get(k) for k in _MANAGED}
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


# ── the write side ───────────────────────────────────────────────────

def test_writes_are_off_unless_asked_for():
    """The kill switch is a default, not a suggestion."""
    cfg = _reload()
    assert cfg.AGENT_ALLOW_WRITES is False


def test_an_empty_value_does_not_turn_writes_on():
    """AGENT_ALLOW_WRITES= in a .env must not read as truthy."""
    cfg = _reload(AGENT_ALLOW_WRITES="")
    assert cfg.AGENT_ALLOW_WRITES is False


def test_the_word_false_does_not_turn_writes_on():
    """bool("false") is True, which is exactly the trap worth a test."""
    for value in ("false", "False", "0", "no", "off"):
        assert _reload(AGENT_ALLOW_WRITES=value).AGENT_ALLOW_WRITES is False


def test_the_usual_ways_of_saying_yes_turn_writes_on():
    for value in ("1", "true", "TRUE", "yes", "on"):
        assert _reload(AGENT_ALLOW_WRITES=value).AGENT_ALLOW_WRITES is True


def test_the_action_timeout_has_a_usable_default():
    cfg = _reload()
    assert cfg.ACTION_TIMEOUT_SECONDS > 0


def test_the_action_timeout_honours_the_env_override():
    cfg = _reload(ACTION_TIMEOUT_SECONDS="5")
    assert cfg.ACTION_TIMEOUT_SECONDS == 5.0


def test_acting_on_the_world_has_a_higher_bar_than_ending_the_loop():
    """0.90 vs CONFIDENCE_THRESHOLD's 0.85 - deliberately not the same number."""
    cfg = _reload()
    assert cfg.AUTO_ACTION_CONFIDENCE == 0.90
    assert cfg.AUTO_ACTION_CONFIDENCE > agent.config.CONFIDENCE_THRESHOLD


def test_the_auto_action_bar_honours_the_env_override():
    """The 3.3 rehearsal exports 0.85; tuning it belongs in Phase 5."""
    cfg = _reload(AUTO_ACTION_CONFIDENCE="0.85")
    assert cfg.AUTO_ACTION_CONFIDENCE == 0.85


# ── tracing ──────────────────────────────────────────────────────────

def test_tracing_is_off_unless_asked_for():
    """Tracing sends the whole transcript to a third party, so opting in is
    explicit - and an offline test run must never try to reach the network."""
    cfg = _reload()
    assert cfg.LANGSMITH_TRACING is False


def test_the_word_false_does_not_turn_tracing_on():
    for value in ("false", "False", "0", "no", "off", ""):
        assert _reload(LANGSMITH_TRACING=value).LANGSMITH_TRACING is False


def test_the_usual_ways_of_saying_yes_turn_tracing_on():
    for value in ("1", "true", "TRUE", "yes", "on"):
        assert _reload(LANGSMITH_TRACING=value).LANGSMITH_TRACING is True


def test_the_older_langchain_variable_still_turns_tracing_on():
    """LANGCHAIN_TRACING_V2 is what most existing setups and docs export, and
    it is the name already sitting in .env files; honouring it costs a line."""
    assert _reload(LANGCHAIN_TRACING_V2="true").LANGSMITH_TRACING is True


def test_the_current_name_wins_over_the_older_one():
    cfg = _reload(LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="true")
    assert cfg.LANGSMITH_TRACING is False


def test_the_project_defaults_to_the_repo_name():
    cfg = _reload()
    assert cfg.LANGSMITH_PROJECT == "sre-agent"


def test_the_project_honours_the_env_override():
    cfg = _reload(LANGSMITH_PROJECT="sre-agent-eval")
    assert cfg.LANGSMITH_PROJECT == "sre-agent-eval"


# ── service_url ──────────────────────────────────────────────────────

def test_a_service_url_defaults_to_the_compose_service_name():
    """Same convention as PROMETHEUS_URL: the compose network is the default."""
    assert agent.config.service_url("data-service") == "http://data-service:8000"


def test_a_service_url_honours_the_per_service_env_override():
    """How a tool run from the host reaches the published port."""
    os.environ["DATA_SERVICE_URL"] = "http://localhost:8002/"
    try:
        assert agent.config.service_url("data-service") == "http://localhost:8002"
    finally:
        os.environ.pop("DATA_SERVICE_URL", None)


def test_an_unknown_service_url_raises_naming_the_known_ones():
    """A pure helper; the tool boundary is what converts this to ok=False."""
    try:
        agent.config.service_url("postgres")
    except ValueError as exc:
        assert "postgres" in str(exc)
        assert "data-service" in str(exc)
    else:
        raise AssertionError("expected a ValueError")
