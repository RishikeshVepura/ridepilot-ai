"""Mock provider endpoints — quotes, bookings, ride status, cancellation.

Thin HTTP layer over :class:`ProviderService`. Each handler validates the request
shape (via Pydantic/query params), delegates to the service, and translates
domain exceptions (core.exceptions) into HTTP status codes. It contains no
business logic or store access.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_provider_service
from core.exceptions import (
    BookingNotFoundError,
    ProviderServiceError,
    RideTypeNotAvailableError,
    UnknownProviderError,
)
from schemas.provider_schemas import (
    BookingRequest,
    BookingResponse,
    CancelResponse,
    QuotesResponse,
    RideStatusResponse,
)
from services.provider_service import ProviderService

router = APIRouter()

# Maps each domain exception to the HTTP status code the API should return.
_STATUS_BY_EXCEPTION: list[tuple[type[ProviderServiceError], int]] = [
    (UnknownProviderError, 404),
    (BookingNotFoundError, 404),
    (RideTypeNotAvailableError, 400),
]


def _to_http_exception(exc: ProviderServiceError) -> HTTPException:
    """Translate a domain exception into the matching HTTPException.

    Args:
        exc: The raised domain error.

    Returns:
        An HTTPException with the mapped status code (500 as a safe default).
    """
    for exc_type, status_code in _STATUS_BY_EXCEPTION:
        if isinstance(exc, exc_type):
            return HTTPException(status_code=status_code, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.get("/{provider}/quotes", response_model=QuotesResponse)
def get_quotes(
    provider: str,
    pickup_lat: float,
    pickup_lng: float,
    dropoff_lat: float,
    dropoff_lng: float,
    service: ProviderService = Depends(get_provider_service),
) -> QuotesResponse:
    """Return current ride quotes for a provider."""
    try:
        return service.get_quotes(
            provider,
            pickup_lat=pickup_lat,
            pickup_lng=pickup_lng,
            dropoff_lat=dropoff_lat,
            dropoff_lng=dropoff_lng,
        )
    except ProviderServiceError as exc:
        raise _to_http_exception(exc) from exc


@router.post("/{provider}/bookings", response_model=BookingResponse)
def create_booking(
    provider: str,
    req: BookingRequest,
    service: ProviderService = Depends(get_provider_service),
) -> BookingResponse:
    """Confirm a booking with the provider."""
    try:
        return service.create_booking(provider, req)
    except ProviderServiceError as exc:
        raise _to_http_exception(exc) from exc


@router.get(
    "/{provider}/bookings/{booking_id}/status",
    response_model=RideStatusResponse,
)
def get_ride_status(
    provider: str,
    booking_id: str,
    service: ProviderService = Depends(get_provider_service),
) -> RideStatusResponse:
    """Get the current ride status for a booking."""
    try:
        return service.get_ride_status(provider, booking_id)
    except ProviderServiceError as exc:
        raise _to_http_exception(exc) from exc


@router.post(
    "/{provider}/bookings/{booking_id}/cancel",
    response_model=CancelResponse,
)
def cancel_booking(
    provider: str,
    booking_id: str,
    service: ProviderService = Depends(get_provider_service),
) -> CancelResponse:
    """Cancel a booking with the provider."""
    try:
        return service.cancel_booking(provider, booking_id)
    except ProviderServiceError as exc:
        raise _to_http_exception(exc) from exc
