"""Database engine, session factory, and schema bootstrap for the AI Service.

The AI Service owns the chat_sessions and chat_messages tables in the shared
PostgreSQL instance. Connection details come from the required DATABASE_URL
environment variable (set via docker-compose / .env). There is no hardcoded
fallback so credentials are never baked into the source tree.

Everything is wrapped in a :class:`Database` class so the engine, session
factory, and schema bootstrap share one configured instance. A module-level
singleton :data:`database` is created for the app and request handlers to share.
"""

from __future__ import annotations

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


class Database:
    """Owns the async engine, session factory, and schema bootstrap.

    Construct once (see the module-level :data:`database` singleton) and share
    across the request handlers and streaming responses so a single connection
    pool is used service-wide.
    """

    def __init__(self, database_url: str | None = None) -> None:
        """Build the async engine and session factory.

        Args:
            database_url: Optional override for the connection URL; defaults to
                the required DATABASE_URL environment value.
        """
        self.database_url = database_url or DATABASE_URL
        # pool_pre_ping avoids handing out stale connections closed by the
        # database/network.
        self.engine = create_async_engine(
            self.database_url, echo=False, pool_pre_ping=True
        )
        # expire_on_commit=False keeps attributes accessible after a commit,
        # convenient when returning ORM objects from request handlers.
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def init_db(self) -> None:
        """Create all AI Service tables if they do not already exist.

        Imports the models module for its side effect of registering every table
        on ``Base.metadata`` before issuing the create call. Safe to run on every
        startup — existing tables are left untouched.
        """
        # Imported for its side effect of registering models on Base.metadata.
        import models.chat_models  # noqa: F401

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


# Shared singleton used by the app (via api.dependencies) and streaming handlers.
database = Database()
