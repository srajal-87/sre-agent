"""Async engine and session factory.

Both are created lazily so that importing ``app.main`` without a DATABASE_URL
still works — which is what keeps the unit tests database-free.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.config import require_database_url

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            require_database_url(),
            pool_size=5,
            max_overflow=0,
            # Supabase's pooler drops idle connections; check before handing one out.
            pool_pre_ping=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker:
    """Return the process-wide session factory, creating it on first use."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def dispose_engine() -> None:
    """Close all pooled connections (called on application shutdown)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
