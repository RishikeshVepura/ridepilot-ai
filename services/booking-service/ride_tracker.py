"""Ride status tracking worker for the Booking Service.

Runs as an asyncio background task started on service startup (see main.py's
lifespan), mirroring the Quote Service monitoring worker. On a configurable
interval it:

    1. Loads every booking that is confirmed/active (CONFIRMED or an in-progress
       ride state) and has a provider_booking_id.
    2. Polls the provider for each booking's ride status via the provider client.
    3. Maps the provider ride status to the Booking lifecycle status.
    4. When the status advances, updates the booking and records the matching
       booking_event (DRIVER_ASSIGNED, DRIVER_ARRIVING, RIDE_STARTED,
       RIDE_COMPLETED, or BOOKING_CANCELLED) with a per-booking sequence.
    5. When the status is unchanged, does nothing for that booking (no duplicate
       events).

The poll interval is read from RIDE_STATUS_POLL_INTERVAL_SECONDS (default 15s),
following the QUOTE_REFRESH_INTERVAL_SECONDS pattern. All provider calls go
through provider_client.py (design rule 6 / Requirement 9.2); the worker never
talks to a provider's HTTP API directly.

The status-mapping core (``compute_transition``) is kept pure and free of any
database or HTTP concerns so it can be reasoned about and tested in isolation.

Requirements:
  6.1 — once a booking is confirmed, the Booking Service tracks its ride status.
  6.2 — track ride status through DRIVER_ASSIGNED, DRIVER_ARRIVING, RIDE_STARTED,
        and RIDE_COMPLETED.
  6.3 — record each milestone so the AI Service can surface and speak it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import provider_client
from db import async_session_factory
from models import Booking, BookingEvent, BookingEventType, BookingStatus
from provider_client import ProviderError

logger = logging.getLogger("booking-service.ride_tracker")

# Default poll cadence in seconds when RIDE_STATUS_POLL_INTERVAL_SECONDS is unset.
# Kept short relative to the mock provider's ride progression so milestones are
# observed promptly.
DEFAULT_POLL_INTERVAL_SECONDS = 15.0

# Booking statuses that represent a confirmed booking whose ride is still in
# progress. These are the bookings the worker actively polls, and the statuses
# that count as "occupying" a provider for the no-two-active-bookings rule
# (Requirement 7.3). Terminal states (RIDE_COMPLETED, CANCELLED, FAILED, EXPIRED)
# are intentionally excluded.
ACTIVE_BOOKING_STATUSES = (
    BookingStatus.CONFIRMED.value,
    BookingStatus.DRIVER_ASSIGNMENT_PENDING.value,
    BookingStatus.DRIVER_ASSIGNED.value,
    BookingStatus.DRIVER_ARRIVING.value,
    BookingStatus.RIDE_STARTED.value,
)

# Monotonic ordering of the booking lifecycle. Used to decide whether a polled
# provider status actually advances the booking (so we never move backwards and
# never record a duplicate event for an unchanged status).
_STATUS_RANK: dict[str, int] = {
    BookingStatus.CREATED.value: 0,
    BookingStatus.PREPARING.value: 1,
    BookingStatus.FINAL_PRICE_VERIFIED.value: 2,
    BookingStatus.WAITING_FOR_CONFIRMATION.value: 3,
    BookingStatus.CONFIRMING.value: 4,
    BookingStatus.CONFIRMED.value: 5,
    BookingStatus.DRIVER_ASSIGNMENT_PENDING.value: 6,
    BookingStatus.DRIVER_ASSIGNED.value: 7,
    BookingStatus.DRIVER_ARRIVING.value: 8,
    BookingStatus.RIDE_STARTED.value: 9,
    BookingStatus.RIDE_COMPLETED.value: 10,
}

# Maps a provider ride stage to the (Booking status, booking_event type) it
# advances the booking to. CANCELLED is handled separately in compute_transition
# because it is terminal rather than a forward step on the ride timeline.
_PROVIDER_STATUS_MAP: dict[str, tuple[str, str]] = {
    "DRIVER_ASSIGNED": (
        BookingStatus.DRIVER_ASSIGNED.value,
        BookingEventType.DRIVER_ASSIGNED.value,
    ),
    "DRIVER_ARRIVING": (
        BookingStatus.DRIVER_ARRIVING.value,
        BookingEventType.DRIVER_ARRIVING.value,
    ),
    "RIDE_STARTED": (
        BookingStatus.RIDE_STARTED.value,
        BookingEventType.RIDE_STARTED.value,
    ),
    "RIDE_COMPLETED": (
        BookingStatus.RIDE_COMPLETED.value,
        BookingEventType.RIDE_COMPLETED.value,
    ),
}


def get_poll_interval_seconds() -> float:
    """Return the ride-status poll interval in seconds.

    Reads RIDE_STATUS_POLL_INTERVAL_SECONDS and falls back to the default when
    the variable is unset or not a positive number.

    Returns:
        The interval in seconds (always > 0).
    """
    raw = os.getenv("RIDE_STATUS_POLL_INTERVAL_SECONDS")
    if raw is None:
        return DEFAULT_POLL_INTERVAL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid RIDE_STATUS_POLL_INTERVAL_SECONDS=%r; using default %ss",
            raw,
            DEFAULT_POLL_INTERVAL_SECONDS,
        )
        return DEFAULT_POLL_INTERVAL_SECONDS
    if value <= 0:
        logger.warning(
            "Non-positive RIDE_STATUS_POLL_INTERVAL_SECONDS=%r; using default %ss",
            raw,
            DEFAULT_POLL_INTERVAL_SECONDS,
        )
        return DEFAULT_POLL_INTERVAL_SECONDS
    return value


def compute_transition(
    current_status: str, provider_status: str
) -> tuple[str, str] | None:
    """Decide how a polled provider status should advance a booking.

    Pure: performs no I/O and has no side effects, so the mapping/ordering rules
    can be reasoned about and tested in isolation.

    A forward ride milestone (DRIVER_ASSIGNED -> DRIVER_ARRIVING -> RIDE_STARTED
    -> RIDE_COMPLETED) only advances the booking when it ranks strictly higher
    than the current status, which prevents moving backwards and prevents
    recording a duplicate event when the status is unchanged. A provider-reported
    CANCELLED transitions any non-cancelled booking to CANCELLED.

    Args:
        current_status: The booking's current status value.
        provider_status: The provider's reported ride stage.

    Returns:
        A (new_status, event_type) tuple of string values when the booking
        should advance, or None when there is nothing to do.
    """
    # Provider-side cancellation is terminal, not a forward milestone.
    if provider_status == "CANCELLED":
        if current_status == BookingStatus.CANCELLED.value:
            return None
        return (
            BookingStatus.CANCELLED.value,
            BookingEventType.BOOKING_CANCELLED.value,
        )

    mapping = _PROVIDER_STATUS_MAP.get(provider_status)
    if mapping is None:
        # Unknown/unhandled provider status — leave the booking untouched.
        return None

    new_status, event_type = mapping
    current_rank = _STATUS_RANK.get(current_status, -1)
    new_rank = _STATUS_RANK.get(new_status, -1)
    if new_rank > current_rank:
        return new_status, event_type
    return None


async def _next_sequence(booking_id: uuid.UUID, db: AsyncSession) -> int:
    """Return the next per-booking event sequence number (max existing + 1)."""
    result = await db.execute(
        select(func.coalesce(func.max(BookingEvent.sequence), 0)).where(
            BookingEvent.booking_id == booking_id
        )
    )
    return int(result.scalar_one()) + 1


async def process_booking(
    booking: Booking, db: AsyncSession, client: httpx.AsyncClient
) -> str | None:
    """Poll one booking's ride status and advance it if the ride progressed.

    Fetches the provider's current ride status, maps it to the booking
    lifecycle, and — only when the status advances — updates the booking and
    records the matching booking_event with the next per-booking sequence number.

    Args:
        booking: The active booking to poll (must have a provider_booking_id).
        db: Active database session.
        client: Shared httpx client for the provider call.

    Returns:
        The new booking status if it advanced, otherwise None.
    """
    if booking.provider_booking_id is None:
        return None

    try:
        ride_status = await provider_client.get_ride_status(
            provider=booking.provider,
            provider_booking_id=booking.provider_booking_id,
            client=client,
        )
    except ProviderError as exc:
        # A provider hiccup must not kill the loop; leave the booking as-is and
        # try again next cycle.
        logger.warning(
            "Provider %s unavailable while tracking booking %s: %s",
            booking.provider,
            booking.id,
            exc.message,
        )
        return None

    transition = compute_transition(booking.status, ride_status.status)
    if transition is None:
        return None

    new_status, event_type = transition
    booking.status = new_status
    await db.flush()

    sequence = await _next_sequence(booking.id, db)
    db.add(
        BookingEvent(
            booking_id=booking.id,
            event_type=event_type,
            sequence=sequence,
            payload={
                "provider": booking.provider,
                "provider_booking_id": booking.provider_booking_id,
                "provider_status": ride_status.status,
                "elapsed_seconds": ride_status.elapsed_seconds,
                "status": new_status,
            },
        )
    )
    await db.commit()

    logger.info(
        "Booking %s advanced to %s (provider status %s)",
        booking.id,
        new_status,
        ride_status.status,
    )
    return new_status


async def run_tracker_cycle(client: httpx.AsyncClient) -> int:
    """Run a single tracking pass over all confirmed/active bookings.

    Each booking is processed in its own database session so one booking's
    failure cannot roll back another's update.

    Args:
        client: Shared httpx client for provider calls.

    Returns:
        The number of bookings whose status advanced this cycle.
    """
    async with async_session_factory() as db:
        result = await db.execute(
            select(Booking.id).where(
                Booking.status.in_(ACTIVE_BOOKING_STATUSES),
                Booking.provider_booking_id.isnot(None),
            )
        )
        booking_ids = [row[0] for row in result.all()]

    advanced = 0
    for booking_id in booking_ids:
        async with async_session_factory() as db:
            # Re-load the booking in this unit of work so updates are tracked and
            # we act on its current status (it may have changed since selection).
            booking = await db.get(Booking, booking_id)
            if (
                booking is None
                or booking.status not in ACTIVE_BOOKING_STATUSES
                or booking.provider_booking_id is None
            ):
                continue
            try:
                if await process_booking(booking, db, client):
                    advanced += 1
            except Exception:  # noqa: BLE001 - keep the worker alive
                logger.exception(
                    "Error tracking ride status for booking %s", booking_id
                )
                await db.rollback()

    return advanced


async def ride_tracker_loop(stop_event: asyncio.Event) -> None:
    """Background loop that polls ride status on the configured interval.

    Runs until ``stop_event`` is set (on service shutdown). A shared httpx client
    is reused across cycles. Any per-cycle error is logged and the loop continues.

    Args:
        stop_event: Set by the lifespan handler to request a graceful stop.
    """
    interval = get_poll_interval_seconds()
    logger.info("Ride tracker worker started (interval=%ss)", interval)

    async with httpx.AsyncClient(
        timeout=provider_client.DEFAULT_TIMEOUT_SECONDS
    ) as client:
        while not stop_event.is_set():
            try:
                await run_tracker_cycle(client)
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception("Ride tracking cycle failed")

            # Sleep for the interval, but wake immediately if asked to stop.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    logger.info("Ride tracker worker stopped")
