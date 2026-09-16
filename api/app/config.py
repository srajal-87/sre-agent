"""Environment-derived configuration.

Bare ``os.getenv`` module constants, matching the convention used by the victim
services (see ``services/api-gateway/app/main.py``).
"""

import os

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool = False) -> bool:
    """Read a boolean env var by an explicit word list.

    Same helper, same word list as ``agent/config.py``: ``bool(os.getenv(...))``
    reads "false" and "0" as True, and a switch that decides whether an endpoint
    spends money on a model is one typo away from doing so.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY


# Supavisor *session* pooler DSN, e.g.
#   postgresql+asyncpg://postgres.<ref>:<pass>@aws-0-<region>.pooler.supabase.com:5432/postgres
# Empty by default so the app (and its unit tests) can be imported without a
# database configured.
DATABASE_URL = os.getenv("DATABASE_URL", "")


# Whether POST /investigate actually runs the agent, or only records the alert
# and opens a pending stub. On by default - recording an incident and never
# investigating it is the less useful half - but a switch, because every run
# costs money and a demo stack may want the endpoint without the bill.
AGENT_AUTO_INVESTIGATE = _flag("AGENT_AUTO_INVESTIGATE", default=True)


def require_database_url() -> str:
    """Return DATABASE_URL, or raise a clear error if it is unset."""
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and set the "
            "Supabase session-pooler DSN."
        )
    return DATABASE_URL
