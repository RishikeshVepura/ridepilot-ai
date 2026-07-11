"""Booking endpoints for the Booking Service.

Thin HTTP layer over :class:`BookingService`. Each handler validates the request
shape (via Pydantic), delegates to the service, and translates domain exceptions
(core.exceptions) into HTTP status codes. It contains no business logic or
database access.

    POST /bookings                 create a booking session
    POST /bookings/{id}/verify     re-verify the final price with the provider
    POST /bookings/{id}/confirm    confirm with idempotency + explicit approval
    POST /bookings/{id}/cancel     cancel a booking
    GET  /bookings/{id}/events     get the ride timeline

Requirements: 5.1–5.6, 6.4, 7.3 (implemented in the service layer).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_booking_service
from core.exceptions import (
    BookingError,
    BookingNotFoundError,
    ConfirmationNotExplicitError,
    ConflictingBookingError,
    InvalidBookingStatusError,
    MissingCoordinatesError,
    ProviderUnavailableError,
)
from schemas.booking_schemas import (
    BookingEventsResponse,
    BookingOut,
    ConfirmBookingRequest,
    ConfirmBookingResponse,
    CreateBookingRequest,
    VerifyBookingResponse,
)
from services.booking_service import BookingService

router = APIRouter(prefix="/bookings", tags=["bookings"])

# Maps each domain exception to the HTTP status code the API should return.
_STATUS_BY_EXCEPTION: list[tuple[type[BookingError], int]] = [
    (BookingNotFoundError, 404),
    (ConfirmationNotExplicitError, 422),
    (InvalidBookingStatusError, 409),
    (ConflictingBookingError, 409),
    (MissingCoordinatesError, 400),
    (ProviderUnavailableError, 502),
]


def _to_http_exception(exc: BookingError) -> HTTPException:
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


@router.post("", response_model=BookingOut, status_code=201)
async def create_booking(
    req: CreateBookingRequest,
    service: BookingService = Depends(get_booking_service),
) -> BookingOut:
    """Create a new booking session (Requirement 5.1)."""
    try:
        booking = await service.create_booking(req)
    except BookingError as exc:
        raise _to_http_exception(exc) from exc
    return BookingOut.model_validate(booking)


@router.post("/{booking_id}/verify", response_model=VerifyBookingResponse)
async def verify_booking(
    booking_id: uuid.UUID,
    service: BookingService = Depends(get_booking_service),
) -> VerifyBookingResponse:
    """Re-verify the final price with the provider (Requirements 5.2, 5.3)."""
    try:
        return await service.verify_booking(booking_id)
    except BookingError as exc:
        raise _to_http_exception(exc) from exc


@router.post("/{booking_id}/confirm", response_model=ConfirmBookingResponse)
async def confirm_booking(
    booking_id: uuid.UUID,
    req: ConfirmBookingRequest,
    service: BookingService = Depends(get_booking_service),
) -> ConfirmBookingResponse:
    """Confirm a booking idempotently and only on explicit approval (5.4–5.6)."""
    try:
        return await service.confirm_booking(booking_id, req)
    except BookingError as exc:
        raise _to_http_exception(exc) from exc


@router.post("/{booking_id}/cancel", response_model=BookingOut)
async def cancel_booking(
    booking_id: uuid.UUID,
    service: BookingService = Depends(get_booking_service),
) -> BookingOut:
    """Cancel a booking (Requirement 6.4)."""
    try:
        booking = await service.cancel_booking(booking_id)
    except BookingError as exc:
        raise _to_http_exception(exc) from exc
    return BookingOut.model_validate(booking)


@router.get("/{booking_id}/events", response_model=BookingEventsResponse)
async def get_booking_events(
    booking_id: uuid.UUID,
    service: BookingService = Depends(get_booking_service),
) -> BookingEventsResponse:
    """Return the ride timeline for a booking."""
    try:
        return await service.get_booking_events(booking_id)
    except BookingError as exc:
        raise _to_http_exception(exc) from exc
