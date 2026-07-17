"""Business logic for the Mock Providers service.

The :class:`ProviderService` implements the four provider operations the Quote
and Booking services call: quoting, confirming a booking, reading ride status,
and cancelling. It orchestrates the provider catalog (static config) and the
in-memory booking store, and models "live" pricing by fluctuating base prices and
ETAs on each call.

It is deliberately HTTP-agnostic: on failure it raises the domain exceptions in
core.exceptions, which the API layer maps to HTTP status codes.
"""

from __future__ import annotations

import random

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
from schemas.provider_schemas import (
    BookingRequest,
    BookingResponse,
    CancelResponse,
    QuoteOption,
    QuotesResponse,
    RideStatusResponse,
)


class ProviderService:
    """Coordinates the provider catalog and booking store to run provider ops."""

    def __init__(self, store: BookingStore, catalog: ProviderCatalog) -> None:
        """Wire the service to its store and catalog.

        Args:
            store: In-memory booking store (shared singleton).
            catalog: Read-only provider/ride-type catalog.
        """
        self.store = store
        self.catalog = catalog

    def get_quotes(
        self,
        provider: str,
        *,
        pickup_lat: float,
        pickup_lng: float,
        dropoff_lat: float,
        dropoff_lng: float,
    ) -> QuotesResponse:
        """Return current ride quotes for a provider.

        Generates a fresh quote for each of the provider's ride types, with price
        and pickup ETA fluctuated to simulate live pricing. Coordinates are
        accepted to mimic a real API but do not affect pricing in this mock.

        Args:
            provider: The provider key (uber, lyft, waymo).
            pickup_lat: Pickup latitude.
            pickup_lng: Pickup longitude.
            dropoff_lat: Dropoff latitude.
            dropoff_lng: Dropoff longitude.

        Returns:
            QuotesResponse containing the provider and a list of QuoteOption.

        Raises:
            UnknownProviderError: If the provider is not simulated.
        """
        self._require_provider(provider)

        quotes = [
            QuoteOption(
                ride_type=rt["ride_type"],
                price=self._fluctuate_price(rt["base_price"]),
                pickup_eta_minutes=self._fluctuate_eta(rt["base_pickup_eta"]),
                trip_duration_minutes=rt["base_trip_duration"],
                available=True,
            )
            for rt in self.catalog.get_ride_types(provider)
        ]

        return QuotesResponse(provider=provider, quotes=quotes)

    def create_booking(self, provider: str, req: BookingRequest) -> BookingResponse:
        """Confirm a booking with the provider.

        Validates the ride type, computes a final price (fluctuated like a real
        provider would at confirmation time), and stores the booking. The booking
        is idempotent on req.idempotency_key.

        Args:
            provider: The provider key.
            req: BookingRequest with ride_type, coords, and idempotency_key.

        Returns:
            BookingResponse with the new provider_booking_id, CONFIRMED status,
            ride type, final price, and pickup ETA.

        Raises:
            UnknownProviderError: If the provider is not simulated.
            RideTypeNotAvailableError: If the provider does not offer the ride type.
        """
        self._require_provider(provider)

        ride_type_config = self.catalog.get_ride_type_config(provider, req.ride_type)
        if ride_type_config is None:
            raise RideTypeNotAvailableError(provider, req.ride_type)

        # Price at booking time — fluctuates like a real final price.
        final_price = self._fluctuate_price(ride_type_config["base_price"])
        pickup_eta = self._fluctuate_eta(ride_type_config["base_pickup_eta"])

        record = self.store.create_booking(
            provider=provider,
            ride_type=req.ride_type,
            price=final_price,
            pickup_eta_minutes=pickup_eta,
            idempotency_key=req.idempotency_key,
        )

        return BookingResponse(
            provider_booking_id=record["provider_booking_id"],
            provider=provider,
            status="CONFIRMED",
            ride_type=record["ride_type"],
            final_price=record["final_price"],
            pickup_eta_minutes=record["pickup_eta_minutes"],
        )

    def get_ride_status(
        self, provider: str, booking_id: str
    ) -> RideStatusResponse:
        """Get the current ride status for a booking.

        Status is computed from elapsed time since confirmation, so repeated
        calls naturally show the ride progressing toward completion.

        Args:
            provider: The provider key.
            booking_id: The provider_booking_id to look up.

        Returns:
            RideStatusResponse with the current status and elapsed seconds.

        Raises:
            UnknownProviderError: If the provider is not simulated.
            BookingNotFoundError: If the booking does not exist.
        """
        self._require_provider(provider)

        status = self.store.get_ride_status(booking_id)
        if status is None:
            raise BookingNotFoundError(booking_id)

        return RideStatusResponse(**status)

    def cancel_booking(self, provider: str, booking_id: str) -> CancelResponse:
        """Cancel a booking with the provider.

        Args:
            provider: The provider key.
            booking_id: The provider_booking_id to cancel.

        Returns:
            CancelResponse with the booking id and CANCELLED status.

        Raises:
            UnknownProviderError: If the provider is not simulated.
            BookingNotFoundError: If the booking does not exist.
        """
        self._require_provider(provider)

        if not self.store.cancel_booking(booking_id):
            raise BookingNotFoundError(booking_id)

        return CancelResponse(provider_booking_id=booking_id, status="CANCELLED")

    def _require_provider(self, provider: str) -> None:
        """Raise UnknownProviderError if the provider is not simulated."""
        if not self.catalog.is_valid(provider):
            raise UnknownProviderError(provider)

    @staticmethod
    def _fluctuate_price(base_price: float) -> float:
        """Apply a random price fluctuation around a base price.

        Simulates real provider pricing that moves between fetches — a uniformly
        random value within ±PRICE_FLUCTUATION of the base price.

        Args:
            base_price: The provider's base price in USD.

        Returns:
            A fluctuated price rounded to 2 decimal places.
        """
        delta = base_price * PRICE_FLUCTUATION
        return round(random.uniform(base_price - delta, base_price + delta), 2)

    @staticmethod
    def _fluctuate_eta(base_eta: int) -> int:
        """Apply a random fluctuation to a pickup ETA.

        Args:
            base_eta: The provider's base pickup ETA in minutes.

        Returns:
            A fluctuated ETA in minutes, clamped to a minimum of 1.
        """
        delta = random.randint(
            -ETA_FLUCTUATION_MINUTES, ETA_FLUCTUATION_MINUTES
        )
        return max(1, base_eta + delta)
