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
