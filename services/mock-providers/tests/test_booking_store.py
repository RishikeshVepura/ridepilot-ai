"""Unit tests for the mock provider's in-memory booking store."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from repositories.booking_store import BookingStore


class BookingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BookingStore()

    @patch("repositories.booking_store.time.time", return_value=1_000.0)
    def test_create_booking_is_idempotent(self, mocked_time) -> None:
        first = self.store.create_booking(
            provider="uber",
            ride_type="UberX",
            price=24.8,
            pickup_eta_minutes=6,
            idempotency_key="retry-safe-key",
        )
        mocked_time.return_value = 1_050.0
        second = self.store.create_booking(
            provider="uber",
            ride_type="UberX",
            price=99.0,
            pickup_eta_minutes=1,
            idempotency_key="retry-safe-key",
        )

        self.assertEqual(second, first)
        self.assertEqual(len(self.store._bookings), 1)
        self.assertEqual(second["final_price"], 24.8)

    @patch("repositories.booking_store.time.time")
    def test_ride_status_progresses_through_lifecycle(self, mocked_time) -> None:
        mocked_time.return_value = 1_000.0
        booking = self.store.create_booking(
            provider="uber",
            ride_type="UberX",
            price=24.8,
            pickup_eta_minutes=6,
            idempotency_key="lifecycle-key",
        )

        for elapsed, expected_status in (
            (0, "DRIVER_ASSIGNED"),
            (10, "DRIVER_ARRIVING"),
            (25, "RIDE_STARTED"),
            (60, "RIDE_COMPLETED"),
        ):
            mocked_time.return_value = 1_000.0 + elapsed
            self.assertEqual(
                self.store.get_ride_status(booking["provider_booking_id"]),
                {
                    "provider_booking_id": booking["provider_booking_id"],
                    "status": expected_status,
                    "elapsed_seconds": elapsed,
                },
            )

    @patch("repositories.booking_store.time.time", return_value=1_000.0)
    def test_cancelled_booking_remains_cancelled(self, mocked_time) -> None:
        booking = self.store.create_booking(
            provider="lyft",
            ride_type="Lyft Standard",
            price=21.9,
            pickup_eta_minutes=10,
            idempotency_key="cancel-key",
        )

        self.assertTrue(self.store.cancel_booking(booking["provider_booking_id"]))
        mocked_time.return_value = 2_000.0
        self.assertEqual(
            self.store.get_ride_status(booking["provider_booking_id"]),
            {
                "provider_booking_id": booking["provider_booking_id"],
                "status": "CANCELLED",
            },
        )

    def test_unknown_booking_cannot_be_read_or_cancelled(self) -> None:
        self.assertIsNone(self.store.get_booking("missing"))
        self.assertIsNone(self.store.get_ride_status("missing"))
        self.assertFalse(self.store.cancel_booking("missing"))


if __name__ == "__main__":
    unittest.main()
