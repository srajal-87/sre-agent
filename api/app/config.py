"""Environment-derived configuration.

Bare ``os.getenv`` module constants, matching the convention used by the victim
services (see ``services/api-gateway/app/main.py``).
"""

import os

# Supavisor *session* pooler DSN, e.g.
#   postgresql+asyncpg://postgres.<ref>:<pass>@aws-0-<region>.pooler.supabase.com:5432/postgres
# Empty by default so the app (and its unit tests) can be imported without a
# database configured.
DATABASE_URL = os.getenv("DATABASE_URL", "")


def require_database_url() -> str:
    """Return DATABASE_URL, or raise a clear error if it is unset."""
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and set the "
            "Supabase session-pooler DSN."
        )
    return DATABASE_URL
