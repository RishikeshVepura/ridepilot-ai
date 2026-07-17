"""FastAPI dependency providers for the Mock Providers service.

The booking store holds in-memory state that must persist across requests, so a
single shared :class:`BookingStore` (and a single :class:`ProviderCatalog`) are
created here and reused. The per-request :class:`ProviderService` is built from
those shared singletons.
"""

from __future__ import annotations

from fastapi import Depends

from catalog.providers import ProviderCatalog
from repositories.booking_store import BookingStore
from services.provider_service import ProviderService

# Shared singletons. The store MUST be shared so bookings persist across requests;
# the catalog is stateless config but shared for consistency.
_booking_store = BookingStore()
_provider_catalog = ProviderCatalog()


def get_booking_store() -> BookingStore:
    """Return the shared in-memory booking store."""
    return _booking_store


def get_provider_catalog() -> ProviderCatalog:
    """Return the shared provider catalog."""
    return _provider_catalog


def get_provider_service(
    store: BookingStore = Depends(get_booking_store),
    catalog: ProviderCatalog = Depends(get_provider_catalog),
) -> ProviderService:
    """Build the service from the shared store and catalog."""
    return ProviderService(store, catalog)
