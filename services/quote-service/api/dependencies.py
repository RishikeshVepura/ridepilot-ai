"""FastAPI dependency providers for the Quote Service.

Wires the layers together per request: a database session feeds a
:class:`QuoteRepository`, which feeds a :class:`QuoteService` that the route
handlers depend on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import database
from repositories.quote_repository import QuoteRepository
from services.quote_service import QuoteService


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a database session scoped to the request.

    Yields:
        An AsyncSession that is closed when the request finishes.
    """
    async with database.session_factory() as session:
        yield session


def get_quote_repository(
    session: AsyncSession = Depends(get_session),
) -> QuoteRepository:
    """Build a repository bound to the request's session."""
    return QuoteRepository(session)


def get_quote_service(
    repository: QuoteRepository = Depends(get_quote_repository),
) -> QuoteService:
    """Build the service from its repository."""
    return QuoteService(repository)
