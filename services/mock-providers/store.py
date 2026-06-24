"""In-memory booking store for mock providers.

Bookings are kept in a simple dict keyed by provider_booking_id.
Ride status is derived from elapsed time since confirmation (time-based progression).
"""

import os
import time
import uuid

# provider_booking_id -> booking record
_bookings: dict[str, dict] = {}

# idempotency_key -> provider_booking_id (so repeat confirms return the same booking)
_idempotency: dict[str, str] = {}


# Ride lifecycle stages and the elapsed-time threshold (seconds) at which each begins.
# Timings scale with RIDE_SPEED_FACTOR so tests can speed up the ride.
# Set RIDE_SPEED_FACTOR=0.5 to make the ride complete twice as fast.
_SPEED = float(os.getenv("RIDE_SPEED_FACTOR", "1.0"))

RIDE_STAGES = [
    (0 * _SPEED, "DRIVER_ASSIGNED"),
    (10 * _SPEED, "DRIVER_ARRIVING"),
    (25 * _SPEED, "RIDE_STARTED"),
    (60 * _SPEED, "RIDE_COMPLETED"),
]


def create_booking(provider: str, ride_type: str, price: float,
                   pickup_eta_minutes: int, idempotency_key: str) -> dict:
    """Create (or return an existing) booking record.

    Confirmation is idempotent: calling this twice with the same idempotency_key
    returns the original booking instead of creating a duplicate. This protects
    against double-bookings from retries.

    Args:
        provider: The provider key (e.g. "uber").
        ride_type: The selected ride type (e.g. "UberX").
        price: The final confirmed price in USD.
        pickup_eta_minutes: Pickup ETA in minutes at confirmation time.
        idempotency_key: Unique key from the caller to dedupe retries.

    Returns:
        The booking record dict. Includes provider_booking_id, provider,
        ride_type, final_price, pickup_eta_minutes, confirmed_at, and cancelled.
    """
    # Idempotency — same key returns the existing booking instead of creating a new one
    if idempotency_key in _idempotency:
        existing_id = _idempotency[idempotency_key]
        return _bookings[existing_id]

    provider_booking_id = f"{provider}_bk_{uuid.uuid4().hex[:8]}"
    record = {
        "provider_booking_id": provider_booking_id,
        "provider": provider,
        "ride_type": ride_type,
        "final_price": price,
        "pickup_eta_minutes": pickup_eta_minutes,
        "confirmed_at": time.time(),
        "cancelled": False,
    }
    _bookings[provider_booking_id] = record
    _idempotency[idempotency_key] = provider_booking_id
    return record


def get_booking(provider_booking_id: str) -> dict | None:
    """Look up a booking record by its provider booking ID.

    Args:
        provider_booking_id: The ID returned when the booking was created.

    Returns:
        The booking record dict, or None if no booking exists with that ID.
    """
    return _bookings.get(provider_booking_id)


def cancel_booking(provider_booking_id: str) -> bool:
    """Mark a booking as cancelled.

    Args:
        provider_booking_id: The ID of the booking to cancel.

    Returns:
        True if the booking existed and was marked cancelled, False if no
        booking was found with that ID.
    """
    booking = _bookings.get(provider_booking_id)
    if not booking:
        return False
    booking["cancelled"] = True
    return True


def get_ride_status(provider_booking_id: str) -> dict | None:
    """Compute the current ride status from elapsed time since confirmation.

    The ride progresses through RIDE_STAGES automatically based on how much
    real time has passed since the booking was confirmed. A cancelled booking
    always reports CANCELLED regardless of elapsed time.

    Args:
        provider_booking_id: The ID of the booking to check.

    Returns:
        A dict with provider_booking_id and status. For active rides it also
        includes elapsed_seconds. Returns None if the booking is not found.
    """
    booking = _bookings.get(provider_booking_id)
    if not booking:
        return None

    if booking["cancelled"]:
        return {"provider_booking_id": provider_booking_id, "status": "CANCELLED"}

    elapsed = time.time() - booking["confirmed_at"]

    # Find the latest stage whose threshold has passed
    status = RIDE_STAGES[0][1]
    for threshold_seconds, stage in RIDE_STAGES:
        if elapsed >= threshold_seconds:
            status = stage

    return {
        "provider_booking_id": provider_booking_id,
        "status": status,
        "elapsed_seconds": int(elapsed),
    }
