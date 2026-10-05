"""Unit tests for booking lifecycle business rules."""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.exceptions import (
    ConfirmationNotExplicitError,
    ConflictingBookingError,
)
from models.booking_models import Booking, BookingEvent, BookingStatus
from provider.provider import ProviderBooking, VerifiedPrice
from schemas.booking_schemas import ConfirmBookingRequest, CreateBookingRequest
from services.booking_service import BookingService


class FakeBookingRepository:
    """Minimal async repository double that retains service-visible state."""

    def __init__(self) -> None:
        self.bookings: dict[uuid.UUID, Booking] = {}
        self.events: list[BookingEvent] = []
        self.pending: list[object] = []
        self.conflicting_booking_id: uuid.UUID | None = None
        self.commits = 0

    def add(self, instance: object) -> None:
        self.pending.append(instance)

    async def flush(self) -> None:
        now = datetime.now(UTC)
        for instance in self.pending:
            if isinstance(instance, Booking):
                if instance.id is None:
                    instance.id = uuid.uuid4()
                if instance.created_at is None:
                    instance.created_at = now
                if instance.updated_at is None:
                    instance.updated_at = now
                self.bookings[instance.id] = instance
            elif isinstance(instance, BookingEvent):
                self.events.append(instance)
        self.pending.clear()

    async def commit(self) -> None:
        await self.flush()
        self.commits += 1

    async def refresh(self, instance: object) -> None:
        return None

    async def get_by_id(self, booking_id: uuid.UUID) -> Booking | None:
        return self.bookings.get(booking_id)

    async def next_event_sequence(self, booking_id: uuid.UUID) -> int:
        return 1 + sum(event.booking_id == booking_id for event in self.events)

    async def find_conflicting_active_booking(
        self, _user_id: str, _provider: str, _exclude_id: uuid.UUID
    ) -> uuid.UUID | None:
        return self.conflicting_booking_id


class StubProviderClient:
    def __init__(self) -> None:
        self.verified_price = VerifiedPrice(
            provider="uber",
            ride_type="UberX",
            price=27.5,
            pickup_eta_minutes=4,
        )
        self.confirmed_booking = ProviderBooking(
            provider="uber",
            provider_booking_id="uber_bk_1234",
            status="CONFIRMED",
            ride_type="UberX",
            final_price=27.5,
            pickup_eta_minutes=4,
        )
        self.verify_calls = 0
        self.confirm_calls = 0
        self.cancel_calls = 0

    async def verify_price(self, **_kwargs: object) -> VerifiedPrice:
        self.verify_calls += 1
        return self.verified_price

    async def confirm_booking(self, **_kwargs: object) -> ProviderBooking:
        self.confirm_calls += 1
        return self.confirmed_booking

    async def cancel_booking(self, **_kwargs: object) -> str:
        self.cancel_calls += 1
        return "CANCELLED"


class BookingServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = FakeBookingRepository()
        self.provider = StubProviderClient()
        self.service = BookingService(self.repository, self.provider)

    async def _create_booking(self, **overrides: object) -> Booking:
        values = {
            "user_id": "user-123",
            "provider": "uber",
            "ride_type": "UberX",
            "selected_price": 24.8,
            "pickup_address": "Pickup",
            "pickup_lat": 37.77,
            "pickup_lng": -122.42,
            "dropoff_address": "Dropoff",
            "dropoff_lat": 37.78,
            "dropoff_lng": -122.41,
        }
        values.update(overrides)
        return await self.service.create_booking(CreateBookingRequest(**values))

    async def test_create_and_verify_booking_records_price_change(self) -> None:
        booking = await self._create_booking()

        verification = await self.service.verify_booking(booking.id)

        self.assertEqual(verification.selected_price, 24.8)
        self.assertEqual(verification.final_price, 27.5)
        self.assertTrue(verification.price_changed)
        self.assertEqual(verification.price_difference, 2.7)
        self.assertEqual(booking.status, BookingStatus.FINAL_PRICE_VERIFIED.value)
        self.assertEqual(self.provider.verify_calls, 1)
        self.assertEqual(
            [event.event_type for event in self.repository.events],
            ["BOOKING_CREATED", "FINAL_PRICE_VERIFIED"],
        )

    async def test_confirm_requires_explicit_approval(self) -> None:
        booking = await self._create_booking()

        with self.assertRaises(ConfirmationNotExplicitError):
            await self.service.confirm_booking(
                booking.id, ConfirmBookingRequest(confirmed=False)
            )

        self.assertEqual(self.provider.confirm_calls, 0)
        self.assertEqual(booking.status, BookingStatus.CREATED.value)

    async def test_confirm_is_idempotent_after_provider_confirmation(self) -> None:
        booking = await self._create_booking()
        request = ConfirmBookingRequest(confirmed=True)

        first = await self.service.confirm_booking(booking.id, request)
        replay = await self.service.confirm_booking(booking.id, request)

        self.assertFalse(first.idempotent_replay)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(first.provider_booking_id, "uber_bk_1234")
        self.assertEqual(self.provider.confirm_calls, 1)
        self.assertEqual(booking.idempotency_key, f"booking-{booking.id}")
        self.assertEqual(
            [event.event_type for event in self.repository.events],
            ["BOOKING_CREATED", "BOOKING_CONFIRMED"],
        )

    async def test_confirm_rejects_an_active_provider_conflict(self) -> None:
        booking = await self._create_booking()
        self.repository.conflicting_booking_id = uuid.uuid4()

        with self.assertRaises(ConflictingBookingError):
            await self.service.confirm_booking(
                booking.id, ConfirmBookingRequest(confirmed=True)
            )

        self.assertEqual(self.provider.confirm_calls, 0)

    async def test_cancel_confirmed_booking_notifies_provider_and_records_event(self) -> None:
        booking = await self._create_booking()
        await self.service.confirm_booking(booking.id, ConfirmBookingRequest(confirmed=True))

        cancelled = await self.service.cancel_booking(booking.id)

        self.assertEqual(cancelled.status, BookingStatus.CANCELLED.value)
        self.assertEqual(self.provider.cancel_calls, 1)
        self.assertEqual(self.repository.events[-1].event_type, "BOOKING_CANCELLED")


if __name__ == "__main__":
    unittest.main()
