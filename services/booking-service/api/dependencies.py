"""FastAPI dependency providers for the Booking Service.

Wires the layers together per request: a database session feeds a
:class:`BookingRepository`, which (together with a shared :class:`ProviderClient`)
feeds a :class:`BookingService` that the route handlers depend on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import database
from provider.provider import ProviderClient
from repositories.booking_repository import BookingRepository
from services.booking_service import BookingService

# One shared provider client for the whole app (stateless HTTP client wrapper).
_provider_client = ProviderClient()


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a database session scoped to the request.

    Yields:
        An AsyncSession that is closed when the request finishes.
    """
    async with database.session_factory() as session:
        yield session


def get_provider_client() -> ProviderClient:
    """Return the shared provider client."""
    return _provider_client


def get_booking_repository(
    session: AsyncSession = Depends(get_session),
) -> BookingRepository:
    """Build a repository bound to the request's session."""
    return BookingRepository(session)


def get_booking_service(
    repository: BookingRepository = Depends(get_booking_repository),
    provider_client: ProviderClient = Depends(get_provider_client),
) -> BookingService:
    """Build the service from its repository and provider client."""
    return BookingService(repository, provider_client)
