"""Unit tests for mock provider business rules."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from catalog.providers import (
    ETA_FLUCTUATION_MINUTES,
    PRICE_FLUCTUATION,
    ProviderCatalog,
)
from core.exceptions import (
    BookingNotFoundError,
    RideTypeNotAvailableError,
    UnknownProviderError,
)
from repositories.booking_store import BookingStore
from schemas.provider_schemas import BookingRequest
from services.provider_service import ProviderService


class ProviderServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ProviderService(BookingStore(), ProviderCatalog())

    @patch("services.provider_service.random.randint", return_value=-3)
    @patch("services.provider_service.random.uniform")
    def test_get_quotes_returns_every_catalogued_ride_type(
        self, mocked_uniform, _mocked_randint
    ) -> None:
        mocked_uniform.side_effect = lambda low, _high: low

        response = self.service.get_quotes(
            "uber",
            pickup_lat=37.77,
            pickup_lng=-122.42,
            dropoff_lat=37.78,
            dropoff_lng=-122.41,
        )

        expected = ProviderCatalog().get_ride_types("uber")
        self.assertEqual(response.provider, "uber")
        self.assertEqual([quote.ride_type for quote in response.quotes], [
            item["ride_type"] for item in expected
        ])
        for quote, config in zip(response.quotes, expected, strict=True):
            self.assertEqual(
                quote.price, round(config["base_price"] * (1 - PRICE_FLUCTUATION), 2)
            )
            self.assertEqual(
                quote.pickup_eta_minutes,
                max(1, config["base_pickup_eta"] - ETA_FLUCTUATION_MINUTES),
            )
            self.assertEqual(quote.trip_duration_minutes, config["base_trip_duration"])

    @patch("services.provider_service.random.randint", return_value=0)
    @patch("services.provider_service.random.uniform", side_effect=lambda low, high: high)
    def test_create_booking_returns_same_record_for_a_retry(
        self, _mocked_uniform, _mocked_randint
    ) -> None:
        request = BookingRequest(
            ride_type="UberX",
            pickup_lat=37.77,
            pickup_lng=-122.42,
            dropoff_lat=37.78,
            dropoff_lng=-122.41,
            idempotency_key="provider-retry-key",
        )

        first = self.service.create_booking("uber", request)
        second = self.service.create_booking("uber", request)

        self.assertEqual(second, first)
        self.assertEqual(first.status, "CONFIRMED")
        self.assertEqual(first.final_price, round(24.8 * (1 + PRICE_FLUCTUATION), 2))

    def test_invalid_provider_and_ride_type_raise_domain_errors(self) -> None:
        with self.assertRaises(UnknownProviderError):
            self.service.get_quotes(
                "taxi", pickup_lat=0, pickup_lng=0, dropoff_lat=0, dropoff_lng=0
            )

        request = BookingRequest(
            ride_type="Not an Uber ride",
            pickup_lat=0,
            pickup_lng=0,
            dropoff_lat=0,
            dropoff_lng=0,
            idempotency_key="invalid-ride-key",
        )
        with self.assertRaises(RideTypeNotAvailableError):
            self.service.create_booking("uber", request)

    def test_missing_booking_raises_domain_errors(self) -> None:
        with self.assertRaises(BookingNotFoundError):
            self.service.get_ride_status("uber", "missing")
        with self.assertRaises(BookingNotFoundError):
            self.service.cancel_booking("uber", "missing")

    def test_eta_fluctuation_never_returns_zero(self) -> None:
        with patch("services.provider_service.random.randint", return_value=-3):
            self.assertEqual(ProviderService._fluctuate_eta(1), 1)


if __name__ == "__main__":
    unittest.main()
