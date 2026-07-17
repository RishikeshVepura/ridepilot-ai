"""Database engine, session factory, and schema bootstrap for the AI Service.

The AI Service owns the chat_sessions and chat_messages tables in the shared
PostgreSQL instance. Connection details come from the required DATABASE_URL
environment variable (set via docker-compose / .env). There is no hardcoded
fallback so credentials are never baked into the source tree.
"""

import os

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# Async SQLAlchemy URL, required from the environment. Set it in .env, e.g.:
#   postgresql+asyncpg://<user>:<password>@postgres:5432/ridepilot
# Failing fast here avoids silently connecting with guessable default
# credentials committed to the repo.
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is required but not set. "
        "Define it in your .env (see .env.example)."
    )


class Base(DeclarativeBase):
    """Declarative base shared by all AI Service ORM models."""


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
    """Create all AI Service tables if they do not already exist.

    Imports the models module so every table is registered on Base.metadata
    before issuing the create call. Safe to run on every startup — existing
    tables are left untouched.
    """
    # Imported for its side effect of registering models on Base.metadata.
    import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
