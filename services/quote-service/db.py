"""Database engine, session factory, and schema bootstrap for the Quote Service.

The Quote Service owns the quote_sessions, quotes, and quote_events tables in
the shared PostgreSQL instance. Connection details come from the DATABASE_URL
environment variable (set via docker-compose / .env) and default to the local
docker-compose Postgres for convenience.
"""

import os

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# Async SQLAlchemy URL. Matches the value in .env / docker-compose:
#   postgresql+asyncpg://ridepilot:ridepilot@postgres:5432/ridepilot
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://ridepilot:ridepilot@postgres:5432/ridepilot",
)


class Base(DeclarativeBase):
    """Declarative base shared by all Quote Service ORM models."""


# Single async engine for the service. pool_pre_ping avoids handing out stale
# connections that were closed by the database/network.
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

# Session factory. expire_on_commit=False keeps attributes accessible after a
# commit, which is convenient when returning ORM objects from request handlers.
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncSession:
    """Yield an async database session for use as a FastAPI dependency.

    Yields:
        An AsyncSession that is automatically closed when the request finishes.
    """
    async with async_session_factory() as session:
        yield session


async def init_db() -> None:
    """Create all Quote Service tables if they do not already exist.

    Imports the models module so every table is registered on Base.metadata
    before issuing the create call. Safe to run on every startup — existing
    tables are left untouched.
    """
    # Imported for its side effect of registering models on Base.metadata.
    import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
