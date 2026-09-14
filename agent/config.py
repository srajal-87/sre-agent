"""Environment-derived configuration for the agent.

Bare ``os.getenv`` module constants, matching the convention used by the API
layer (see ``api/app/config.py``) and the victim services. Every constant has a
usable default so that ``import agent.config`` never fails, which is what keeps
the unit tests free of Docker, Prometheus, and a database.
"""

import os

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
