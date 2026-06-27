"""Booking endpoints for the Booking Service.

Exposes the booking lifecycle the AI Service drives:

    POST /bookings                 create a booking session
    POST /bookings/{id}/verify     re-verify the final price with the provider
    POST /bookings/{id}/confirm    confirm with idempotency + explicit approval
    POST /bookings/{id}/cancel     cancel a booking
    GET  /bookings/{id}/events     get the ride timeline

All provider calls go through provider_client.py (design rule 6 / Requirement
9.2); the handlers never talk to a provider's HTTP API directly. Every
meaningful action is recorded as a booking_event so a booking's history is
captured end to end. Events are numbered per booking starting at 1.

Requirements:
  5.1 — create a booking session when a user initiates booking.
  5.2 — re-verify the final price with the provider before confirmation.
  5.3 — surface a changed final price so re-confirmation can be required.
  5.4 — confirm the booking with the provider on explicit approval.
  5.5 — use an idempotency key to prevent duplicate bookings.
  5.6 — never confirm without explicit user approval.
  6.4 — cancel a confirmed booking with the provider and mark it CANCELLED.
  7.3 — a user may not hold two concurrent active bookings with the same provider.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from models import Booking, BookingEvent, BookingEventType, BookingStatus
from provider_client import ProviderError
import provider_client
from ride_tracker import ACTIVE_BOOKING_STATUSES
from schemas import (
    BookingEventOut,
    BookingEventsResponse,
    BookingOut,
    ConfirmBookingRequest,
    ConfirmBookingResponse,
    CreateBookingRequest,
    VerifyBookingResponse,
)

router = APIRouter(prefix="/bookings", tags=["bookings"])


async def _get_booking_or_404(booking_id: uuid.UUID, db: AsyncSession) -> Booking:
    """Load a booking by id or raise 404.

    Args:
        booking_id: The booking id from the path.
        db: Active database session.

    Returns:
        The Booking ORM object.

    Raises:
        HTTPException: 404 if no booking with that id exists.
    """
    booking = await db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


async def _assert_no_conflicting_active_booking(
    booking: Booking, db: AsyncSession
) -> None:
    """Reject confirmation when the user already has an active ride on this provider.

    A user must not hold two concurrent active bookings with the same provider:
    while one ride is confirmed or in progress (CONFIRMED, DRIVER_ASSIGNMENT_PENDING,
    DRIVER_ASSIGNED, DRIVER_ARRIVING, RIDE_STARTED) the same provider cannot be
    booked again. Terminal bookings (CANCELLED, RIDE_COMPLETED, FAILED, EXPIRED)
    do not count, so the user can re-book the same provider once the prior ride
    ends. (Requirement 7.3)

    Args:
        booking: The booking about to be confirmed.
        db: Active database session.

    Raises:
        HTTPException: 409 if the user already has an active booking for this
            provider (other than this booking).
    """
    result = await db.execute(
        select(Booking.id).where(
            Booking.user_id == booking.user_id,
            Booking.provider == booking.provider,
            Booking.id != booking.id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
        )
    )
    conflicting_id = result.scalars().first()
    if conflicting_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"User already has an active booking with provider "
                f"'{booking.provider}' (booking {conflicting_id})"
            ),
        )


async def _next_sequence(booking_id: uuid.UUID, db: AsyncSession) -> int:
    """Return the next event sequence number for a booking.

    Events are numbered per booking starting at 1 so their order is stable and
    independent of timestamp resolution.

    Args:
        booking_id: The booking the event belongs to.
        db: Active database session.

    Returns:
        The next sequence integer (max existing + 1, or 1 if none yet).
    """
    result = await db.execute(
        select(func.coalesce(func.max(BookingEvent.sequence), 0)).where(
            BookingEvent.booking_id == booking_id
        )
    )
    return int(result.scalar_one()) + 1


def _require_coords(booking: Booking) -> tuple[float, float, float, float]:
    """Return pickup/dropoff coordinates or raise 400 if any are missing.

    Pickup/dropoff coordinates are optional on a booking but are required to
    talk to the provider for verification or confirmation.

    Args:
        booking: The booking to read coordinates from.

    Returns:
        A (pickup_lat, pickup_lng, dropoff_lat, dropoff_lng) tuple of floats.

    Raises:
        HTTPException: 400 if any coordinate is missing.
    """
    if (
        booking.pickup_lat is None
        or booking.pickup_lng is None
        or booking.dropoff_lat is None
        or booking.dropoff_lng is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Pickup and dropoff coordinates are required for this action",
        )
    return (
        float(booking.pickup_lat),
        float(booking.pickup_lng),
        float(booking.dropoff_lat),
        float(booking.dropoff_lng),
    )


@router.post("", response_model=BookingOut, status_code=201)
async def create_booking(
    req: CreateBookingRequest, db: AsyncSession = Depends(get_session)
) -> Booking:
    """Create a new booking session.

    Persists the selected quote's context so the booking can later be
    re-verified and confirmed with the provider, and records a BOOKING_CREATED
    event. The booking starts in CREATED status. (Requirement 5.1)

    Args:
        req: The booking details (user, provider, ride type, selected price,
            pickup/dropoff context).
        db: Active database session.

    Returns:
        The newly created Booking.
    """
    booking = Booking(
        user_id=req.user_id,
        chat_session_id=req.chat_session_id,
        quote_session_id=req.quote_session_id,
        quote_id=req.quote_id,
        provider=req.provider,
        ride_type=req.ride_type,
        pickup_address=req.pickup_address,
        pickup_lat=req.pickup_lat,
        pickup_lng=req.pickup_lng,
        dropoff_address=req.dropoff_address,
        dropoff_lat=req.dropoff_lat,
        dropoff_lng=req.dropoff_lng,
        selected_price=req.selected_price,
        currency=req.currency,
        pickup_eta_minutes=req.pickup_eta_minutes,
        status=BookingStatus.CREATED.value,
    )
    db.add(booking)
    await db.flush()

    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type=BookingEventType.BOOKING_CREATED.value,
            sequence=1,
            payload={
                "user_id": req.user_id,
                "provider": req.provider,
                "ride_type": req.ride_type,
                "selected_price": req.selected_price,
                "currency": req.currency,
            },
        )
    )

    await db.commit()
    await db.refresh(booking)
    return booking


@router.post("/{booking_id}/verify", response_model=VerifyBookingResponse)
async def verify_booking(
    booking_id: uuid.UUID, db: AsyncSession = Depends(get_session)
) -> VerifyBookingResponse:
    """Re-verify the final price with the provider before confirmation.

    Loads the booking, re-quotes the provider for the selected ride type via the
    provider client, stores the verified final price, and moves the booking to
    FINAL_PRICE_VERIFIED. The response surfaces the selected vs. final price and
    whether they differ so the caller can require explicit re-confirmation.
    (Requirements 5.2, 5.3)

    Args:
        booking_id: The booking to verify.
        db: Active database session.

    Returns:
        VerifyBookingResponse with the booking, both prices, and the delta.

    Raises:
        HTTPException: 404 if the booking does not exist, 400 if coordinates are
            missing, or 502 if the provider call fails.
    """
    booking = await _get_booking_or_404(booking_id, db)
    pickup_lat, pickup_lng, dropoff_lat, dropoff_lng = _require_coords(booking)

    try:
        verified = await provider_client.verify_price(
            provider=booking.provider,
            ride_type=booking.ride_type,
            pickup_lat=pickup_lat,
            pickup_lng=pickup_lng,
            dropoff_lat=dropoff_lat,
            dropoff_lng=dropoff_lng,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    selected_price = float(booking.selected_price)
    final_price = float(verified.price)
    price_difference = round(final_price - selected_price, 2)
    price_changed = price_difference != 0

    booking.final_price = final_price
    booking.currency = verified.currency
    if verified.pickup_eta_minutes is not None:
        booking.pickup_eta_minutes = verified.pickup_eta_minutes
    booking.status = BookingStatus.FINAL_PRICE_VERIFIED.value
    await db.flush()

    sequence = await _next_sequence(booking_id, db)
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type=BookingEventType.FINAL_PRICE_VERIFIED.value,
            sequence=sequence,
            payload={
                "selected_price": selected_price,
                "final_price": final_price,
                "price_changed": price_changed,
                "price_difference": price_difference,
                "currency": verified.currency,
            },
        )
    )

    await db.commit()
    await db.refresh(booking)

    return VerifyBookingResponse(
        booking=BookingOut.model_validate(booking),
        selected_price=selected_price,
        final_price=final_price,
        price_changed=price_changed,
        price_difference=price_difference,
        currency=verified.currency,
    )


@router.post("/{booking_id}/confirm", response_model=ConfirmBookingResponse)
async def confirm_booking(
    booking_id: uuid.UUID,
    req: ConfirmBookingRequest,
    db: AsyncSession = Depends(get_session),
) -> ConfirmBookingResponse:
    """Confirm a booking with the provider, idempotently and only on approval.

    Confirmation is never implicit: ``confirmed`` must be explicitly true or the
    request is rejected with 422 (Requirement 5.6). A stable idempotency key is
    derived from the booking id when the caller does not supply one and is
    persisted on the booking so retries never create a duplicate (Requirement
    5.5). If the booking is already CONFIRMED with a provider_booking_id, the
    existing booking is returned with ``idempotent_replay=true`` without creating
    a second provider booking. Otherwise the provider is asked to confirm, the
    provider_booking_id and final price are stored, and the booking moves to
    CONFIRMED. (Requirements 5.4, 5.5, 5.6)

    Args:
        booking_id: The booking to confirm.
        req: The confirmation request carrying explicit approval and an optional
            idempotency key.
        db: Active database session.

    Returns:
        ConfirmBookingResponse with the booking, provider_booking_id, final
        price, and whether this was an idempotent replay.

    Raises:
        HTTPException: 404 if the booking does not exist, 422 if confirmation is
            not explicit, 400 if coordinates are missing, or 502 if the provider
            call fails.
    """
    booking = await _get_booking_or_404(booking_id, db)

    # Explicit confirmation is mandatory — never confirm implicitly.
    if not req.confirmed:
        raise HTTPException(
            status_code=422,
            detail="Explicit confirmation is required (confirmed must be true)",
        )

    # Cancelled/failed bookings cannot be confirmed.
    if booking.status in (
        BookingStatus.CANCELLED.value,
        BookingStatus.FAILED.value,
        BookingStatus.EXPIRED.value,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot confirm a booking in status {booking.status}",
        )

    # Stable idempotency key: caller-supplied, else derived from the booking id
    # so retries of the same booking always dedupe to the same provider booking.
    idempotency_key = req.idempotency_key or f"booking-{booking.id}"
    if booking.idempotency_key is None:
        booking.idempotency_key = idempotency_key
        await db.flush()
    else:
        # Once persisted, the booking's own key is authoritative for retries.
        idempotency_key = booking.idempotency_key

    # Idempotent replay: already confirmed with a provider booking — return it
    # without creating a second provider booking.
    if (
        booking.status == BookingStatus.CONFIRMED.value
        and booking.provider_booking_id is not None
    ):
        return ConfirmBookingResponse(
            booking=BookingOut.model_validate(booking),
            provider_booking_id=booking.provider_booking_id,
            final_price=float(booking.final_price)
            if booking.final_price is not None
            else float(booking.selected_price),
            currency=booking.currency,
            idempotent_replay=True,
        )

    pickup_lat, pickup_lng, dropoff_lat, dropoff_lng = _require_coords(booking)

    # A user may not hold two concurrent active bookings with the same provider.
    # Checked just before confirming so verified-but-unconfirmed bookings don't
    # block each other. (Requirement 7.3)
    await _assert_no_conflicting_active_booking(booking, db)

    try:
        provider_booking = await provider_client.confirm_booking(
            provider=booking.provider,
            ride_type=booking.ride_type,
            idempotency_key=idempotency_key,
            pickup_lat=pickup_lat,
            pickup_lng=pickup_lng,
            dropoff_lat=dropoff_lat,
            dropoff_lng=dropoff_lng,
        )
    except ProviderError as exc:
        # Leave the booking in its current (pre-confirmation) state rather than
        # marking it CONFIRMED. The persisted idempotency key makes a retry safe.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    booking.provider_booking_id = provider_booking.provider_booking_id
    booking.final_price = float(provider_booking.final_price)
    booking.currency = provider_booking.currency
    if provider_booking.pickup_eta_minutes is not None:
        booking.pickup_eta_minutes = provider_booking.pickup_eta_minutes
    booking.status = BookingStatus.CONFIRMED.value
    await db.flush()

    sequence = await _next_sequence(booking_id, db)
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type=BookingEventType.BOOKING_CONFIRMED.value,
            sequence=sequence,
            payload={
                "provider_booking_id": provider_booking.provider_booking_id,
                "final_price": float(provider_booking.final_price),
                "currency": provider_booking.currency,
                "idempotency_key": idempotency_key,
            },
        )
    )

    await db.commit()
    await db.refresh(booking)

    return ConfirmBookingResponse(
        booking=BookingOut.model_validate(booking),
        provider_booking_id=provider_booking.provider_booking_id,
        final_price=float(provider_booking.final_price),
        currency=provider_booking.currency,
        idempotent_replay=False,
    )


@router.post("/{booking_id}/cancel", response_model=BookingOut)
async def cancel_booking(
    booking_id: uuid.UUID, db: AsyncSession = Depends(get_session)
) -> Booking:
    """Cancel a booking.

    If the booking was confirmed with a provider, the provider is asked to
    cancel it first. The booking is then marked CANCELLED and a BOOKING_CANCELLED
    event is recorded. Cancelling an already-cancelled booking is a no-op that
    returns the current state. (Requirement 6.4)

    Args:
        booking_id: The booking to cancel.
        db: Active database session.

    Returns:
        The cancelled Booking.

    Raises:
        HTTPException: 404 if the booking does not exist, or 502 if the provider
            cancellation fails.
    """
    booking = await _get_booking_or_404(booking_id, db)

    # Cancelling an already-cancelled booking is a no-op.
    if booking.status == BookingStatus.CANCELLED.value:
        return booking

    previous_status = booking.status

    # Only call the provider if there is a confirmed provider booking to cancel.
    if (
        booking.status == BookingStatus.CONFIRMED.value
        and booking.provider_booking_id is not None
    ):
        try:
            await provider_client.cancel_booking(
                provider=booking.provider,
                provider_booking_id=booking.provider_booking_id,
            )
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    booking.status = BookingStatus.CANCELLED.value
    await db.flush()

    sequence = await _next_sequence(booking_id, db)
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type=BookingEventType.BOOKING_CANCELLED.value,
            sequence=sequence,
            payload={
                "previous_status": previous_status,
                "provider_booking_id": booking.provider_booking_id,
            },
        )
    )

    await db.commit()
    await db.refresh(booking)
    return booking


@router.get("/{booking_id}/events", response_model=BookingEventsResponse)
async def get_booking_events(
    booking_id: uuid.UUID, db: AsyncSession = Depends(get_session)
) -> BookingEventsResponse:
    """Return the ride timeline for a booking.

    Events are returned ordered by sequence then created_at so the timeline is
    stable even when several events share a timestamp.

    Args:
        booking_id: The booking whose events to return.
        db: Active database session.

    Returns:
        BookingEventsResponse with the ordered list of events.

    Raises:
        HTTPException: 404 if the booking does not exist.
    """
    await _get_booking_or_404(booking_id, db)

    result = await db.execute(
        select(BookingEvent)
        .where(BookingEvent.booking_id == booking_id)
        .order_by(BookingEvent.sequence.asc(), BookingEvent.created_at.asc())
    )
    events = result.scalars().all()

    return BookingEventsResponse(
        booking_id=booking_id,
        events=[BookingEventOut.model_validate(e) for e in events],
    )
