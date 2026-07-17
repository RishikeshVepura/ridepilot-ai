"""In-memory booking store for the Mock Providers service.

The :class:`BookingStore` is this service's data-access layer. Since the mock
providers have no database, bookings are held in memory keyed by
provider_booking_id, and ride status is derived from elapsed time since
confirmation (time-based progression). A single shared instance must be used for
the life of the process (see api.dependencies), so state persists across requests.
"""

from __future__ import annotations

import os
import time
import uuid


class BookingStore:
    """In-memory store of provider bookings with time-based ride progression."""

    def __init__(self, speed_factor: float | None = None) -> None:
        """Initialize empty stores and the ride-stage timings.

        Args:
            speed_factor: Optional override for RIDE_SPEED_FACTOR. Stage
                thresholds scale with this so a ride can be sped up for testing
                (e.g. 0.5 completes twice as fast). Defaults to the
                RIDE_SPEED_FACTOR env value, or 1.0.
        """
        # provider_booking_id -> booking record
        self._bookings: dict[str, dict] = {}
        # idempotency_key -> provider_booking_id (repeat confirms return the same booking)
        self._idempotency: dict[str, str] = {}

        speed = (
            speed_factor
            if speed_factor is not None
            else float(os.getenv("RIDE_SPEED_FACTOR", "1.0"))
        )
        # Ride lifecycle stages and the elapsed-time threshold (seconds) at which
        # each begins. Timings scale with the speed factor.
        self._ride_stages: list[tuple[float, str]] = [
            (0 * speed, "DRIVER_ASSIGNED"),
            (10 * speed, "DRIVER_ARRIVING"),
            (25 * speed, "RIDE_STARTED"),
            (60 * speed, "RIDE_COMPLETED"),
        ]

    def create_booking(
        self,
        provider: str,
        ride_type: str,
        price: float,
        pickup_eta_minutes: int,
        idempotency_key: str,
    ) -> dict:
        """Create (or return an existing) booking record.

        Confirmation is idempotent: calling this twice with the same
        idempotency_key returns the original booking instead of creating a
        duplicate, protecting against double-bookings from retries.

        Args:
            provider: The provider key (e.g. "uber").
            ride_type: The selected ride type (e.g. "UberX").
            price: The final confirmed price in USD.
            pickup_eta_minutes: Pickup ETA in minutes at confirmation time.
            idempotency_key: Unique key from the caller to dedupe retries.

        Returns:
            The booking record dict.
        """
        # Idempotency — same key returns the existing booking.
        if idempotency_key in self._idempotency:
            existing_id = self._idempotency[idempotency_key]
            return self._bookings[existing_id]

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
        self._bookings[provider_booking_id] = record
        self._idempotency[idempotency_key] = provider_booking_id
        return record

    def get_booking(self, provider_booking_id: str) -> dict | None:
        """Look up a booking record by its provider booking id.

        Args:
            provider_booking_id: The id returned when the booking was created.

        Returns:
            The booking record dict, or None if no booking exists with that id.
        """
        return self._bookings.get(provider_booking_id)

    def cancel_booking(self, provider_booking_id: str) -> bool:
        """Mark a booking as cancelled.

        Args:
            provider_booking_id: The id of the booking to cancel.

        Returns:
            True if the booking existed and was marked cancelled, False if no
            booking was found with that id.
        """
        booking = self._bookings.get(provider_booking_id)
        if not booking:
            return False
        booking["cancelled"] = True
        return True

    def get_ride_status(self, provider_booking_id: str) -> dict | None:
        """Compute the current ride status from elapsed time since confirmation.

        The ride progresses through the ride stages automatically based on how
        much real time has passed since the booking was confirmed. A cancelled
        booking always reports CANCELLED regardless of elapsed time.

        Args:
            provider_booking_id: The id of the booking to check.

        Returns:
            A dict with provider_booking_id and status. For active rides it also
            includes elapsed_seconds. Returns None if the booking is not found.
        """
        booking = self._bookings.get(provider_booking_id)
        if not booking:
            return None

        if booking["cancelled"]:
            return {
                "provider_booking_id": provider_booking_id,
                "status": "CANCELLED",
            }

        elapsed = time.time() - booking["confirmed_at"]

        # Find the latest stage whose threshold has passed.
        status = self._ride_stages[0][1]
        for threshold_seconds, stage in self._ride_stages:
            if elapsed >= threshold_seconds:
                status = stage

        return {
            "provider_booking_id": provider_booking_id,
            "status": status,
            "elapsed_seconds": int(elapsed),
        }
