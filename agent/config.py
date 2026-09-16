"""Environment-derived configuration for the agent.

Bare ``os.getenv`` module constants, matching the convention used by the API
layer (see ``api/app/config.py``) and the victim services. Every constant has a
usable default so that ``import agent.config`` never fails, which is what keeps
the unit tests free of Docker, Prometheus, and a database.
"""

import os

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool = False) -> bool:
    """Read a boolean env var by an explicit word list.

    ``bool(os.getenv(...))`` would read "false" and "0" as True, which for a
    switch that decides whether the agent may touch the running stack is one
    typo away from a real restart.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY


# Base URL of the Prometheus server, no trailing slash. The default is the
# compose service name; override with PROMETHEUS_URL=http://localhost:9090 when
# running a tool from the host rather than from inside the network.
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")

# Compose project name, used to resolve containers by label rather than by
# container name. Docker Compose defaults it to the directory holding the
# compose file, so this matches an unmodified checkout.
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT", "sre-agent")

# The services whose stdout is worth reading. Deliberately excludes prometheus
# and agent-api: the agent does not investigate itself.
LOG_SERVICES = ["api-gateway", "data-service", "downstream-dep"]

# Ports the victim services publish to the host, used only to document the
# override in .env.example — inside the compose network every service listens
# on 8000 and is addressed by name.
SERVICE_PORTS = {"api-gateway": 8001, "data-service": 8002, "downstream-dep": 8003}


def service_url(name: str) -> str:
    """Return the base URL of a victim service, no trailing slash.

    Defaults to the compose service name, exactly like PROMETHEUS_URL: inside
    the network that is what resolves. Running a tool from the host instead,
    override per service with e.g. DATA_SERVICE_URL=http://localhost:8002.

    Raises for an unknown name — a pure helper, so the tool boundary is what
    turns this into an ``ok=False`` observation.
    """
    if name not in SERVICE_PORTS:
        raise ValueError(
            f"unknown service '{name}'; known: {sorted(SERVICE_PORTS)}"
        )
    override = os.getenv(name.upper().replace("-", "_") + "_URL")
    if override and override.strip():
        return override.strip().rstrip("/")
    return f"http://{name}:8000"

# Reused verbatim from the API layer rather than redefined, so both read the one
# Supabase DSN. Empty by default; the deploy tool reports a readable error
# instead of raising at import time.
DATABASE_URL = os.getenv("DATABASE_URL", "")

# The model that does the reasoning, addressed as a Bedrock model id. The "eu."
# prefix is the EU cross-region inference profile, which is how Claude models
# are reached in eu-north-1; if Bedrock 404s it, try the bare
# "anthropic.claude-sonnet-4-5-20250929-v1:0" via AGENT_MODEL before editing
# code. See agent/graph/llm.py for why thinking is never disabled here.
AGENT_MODEL = os.getenv("AGENT_MODEL", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")

# The Bedrock region, passed to the client explicitly. Doing so keeps the SDK's
# region inference - and the boto3 import behind it - off the path entirely.
BEDROCK_REGION = os.getenv("AWS_REGION", "eu-north-1")

# Ceiling on one response, thinking tokens included. Comfortably above what a
# hypothesis plus three tool calls needs, and low enough to stay well inside the
# SDK's non-streaming timeout.
AGENT_MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "16000"))

# Extended-thinking budget. Sonnet 4.5 takes an explicit budget rather than the
# adaptive thinking of the Opus 5 family. Two hard constraints: it must be
# >= 1024, and strictly < AGENT_MAX_TOKENS, which the budget is drawn from.
AGENT_THINKING_BUDGET = int(os.getenv("AGENT_THINKING_BUDGET", "4000"))

# How many read-tool calls the executor will run for one reason cycle. Token
# discipline: a model asked for an unbounded list will happily produce one. The
# system prompt states this same number, so the model never plans work that
# gather_evidence would silently drop.
MAX_TOOL_CALLS_PER_ITERATION = int(os.getenv("MAX_TOOL_CALLS_PER_ITERATION", "3"))

# -- stop conditions --------------------------------------------------
#
# All six are deterministic and all are evaluated in graph/nodes.py's decide.
# Every one of these numbers is a guess until Phase 5 gives a baseline, which is
# why they are overridable rather than literals in the code.

# Reason cycles before the loop gives up. Each of the four fault types is
# distinguishable in two or three probes, so six is headroom, not a working
# limit.
MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "6"))

# High enough that a single-signal guess cannot clear it, low enough that a
# correctly diagnosed fault does.
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.85"))

# The ceiling a hypothesis is held to when its citations do not resolve to
# queries that were actually issued. Deliberately below CONFIDENCE_THRESHOLD:
# an uncited claim must not be able to end the investigation.
UNCITED_CONFIDENCE_CAP = float(os.getenv("UNCITED_CONFIDENCE_CAP", "0.6"))

# Movement smaller than this across two consecutive cycles counts as a plateau:
# more looking has stopped changing the answer.
CONFIDENCE_PLATEAU = float(os.getenv("CONFIDENCE_PLATEAU", "0.05"))

COST_CAP_USD = float(os.getenv("COST_CAP_USD", "0.50"))
WALL_CLOCK_CAP_SECONDS = float(os.getenv("WALL_CLOCK_CAP_SECONDS", "120"))

# Consecutive failed model calls before the run is abandoned. One is a blip.
MAX_CONSECUTIVE_LLM_ERRORS = int(os.getenv("MAX_CONSECUTIVE_LLM_ERRORS", "2"))

# -- the audit trail --------------------------------------------------

# Whether a run is traced to LangSmith. Off by default: tracing ships the whole
# transcript to a third party, and the offline test suite must never reach the
# network. LANGCHAIN_TRACING_V2 is the older spelling of the same switch and is
# what most existing setups export, so it is honoured as a fallback - the
# current name wins when both are set.
LANGSMITH_TRACING = _flag("LANGSMITH_TRACING", default=_flag("LANGCHAIN_TRACING_V2"))

# The LangSmith project traces are filed under. Matching the repo name keeps a
# fresh clone's traces somewhere findable without any configuration.
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "sre-agent") or "sre-agent"

# -- the write side ---------------------------------------------------

# The kill switch. Off means the policy gate still runs and still records its
# decision, but every action reports executed=False, dry_run=True. Nothing in
# the stack is touched unless this is deliberately turned on.
AGENT_ALLOW_WRITES = _flag("AGENT_ALLOW_WRITES")

# Ceiling on one write action, verification included. A container restart plus
# a health poll is seconds; this is the "something is wedged" bound.
ACTION_TIMEOUT_SECONDS = float(os.getenv("ACTION_TIMEOUT_SECONDS", "30"))

# The confidence an investigation must reach before the policy gate will act on
# the world. Deliberately above CONFIDENCE_THRESHOLD (0.85): ending a loop and
# restarting a service are not the same bet.
#
# Known tension, recorded rather than hidden: at 0.90 none of the Day 12
# rehearsal runs would have auto-acted. 0.90 ships as the conservative default
# and the 3.3 rehearsal exports 0.85 to prove the write path end to end. Tuning
# it belongs in Phase 5, against data rather than a guess.
AUTO_ACTION_CONFIDENCE = float(os.getenv("AUTO_ACTION_CONFIDENCE", "0.90"))
