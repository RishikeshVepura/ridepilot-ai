"""Mock provider endpoints — quotes, bookings, ride status, cancellation."""

import random

from fastapi import APIRouter, HTTPException

import providers
import store
from schemas import (
    BookingRequest,
    BookingResponse,
    CancelResponse,
    QuoteOption,
    QuotesResponse,
    RideStatusResponse,
)

router = APIRouter()


def _fluctuate_price(base_price: float) -> float:
    """Apply a random price fluctuation around a base price.

    Simulates real provider pricing that moves between fetches. The result is
    a uniformly random value within ±PRICE_FLUCTUATION of the base price.

    Args:
        base_price: The provider's base price in USD.

    Returns:
        A fluctuated price rounded to 2 decimal places.
    """
    delta = base_price * providers.PRICE_FLUCTUATION
    return round(random.uniform(base_price - delta, base_price + delta), 2)


def _fluctuate_eta(base_eta: int) -> int:
    """Apply a random fluctuation to a pickup ETA.

    Args:
        base_eta: The provider's base pickup ETA in minutes.

    Returns:
        A fluctuated ETA in minutes, clamped to a minimum of 1.
    """
    delta = random.randint(-providers.ETA_FLUCTUATION_MINUTES, providers.ETA_FLUCTUATION_MINUTES)
    return max(1, base_eta + delta)


def _validate_provider(provider: str):
    """Raise a 404 if the provider is not one we simulate.

    Args:
        provider: The provider key from the request path.

    Raises:
        HTTPException: 404 if the provider is unknown.
    """
    if not providers.is_valid_provider(provider):
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")


@router.get("/{provider}/quotes", response_model=QuotesResponse)
def get_quotes(
    provider: str,
    pickup_lat: float,
    pickup_lng: float,
    dropoff_lat: float,
    dropoff_lng: float,
):
    """Return current ride quotes for a provider.

    Generates a fresh quote for each of the provider's ride types, with price
    and pickup ETA fluctuated to simulate live pricing. Coordinates are accepted
    to mimic a real API but do not affect pricing in this mock.

    Args:
        provider: The provider key from the path (uber, lyft, waymo).
        pickup_lat: Pickup latitude.
        pickup_lng: Pickup longitude.
        dropoff_lat: Dropoff latitude.
        dropoff_lng: Dropoff longitude.

    Returns:
        QuotesResponse containing the provider and a list of QuoteOption.

    Raises:
        HTTPException: 404 if the provider is unknown.
    """
    _validate_provider(provider)

    quotes = []
    for rt in providers.get_ride_types(provider):
        quotes.append(
            QuoteOption(
                ride_type=rt["ride_type"],
                price=_fluctuate_price(rt["base_price"]),
                pickup_eta_minutes=_fluctuate_eta(rt["base_pickup_eta"]),
                trip_duration_minutes=rt["base_trip_duration"],
                available=True,
            )
        )

    return QuotesResponse(provider=provider, quotes=quotes)


@router.post("/{provider}/bookings", response_model=BookingResponse)
def create_booking(provider: str, req: BookingRequest):
    """Confirm a booking with the provider.

    Validates the ride type, computes a final price (fluctuated like a real
    provider would at confirmation time), and stores the booking. The booking
    is idempotent on req.idempotency_key.

    Args:
        provider: The provider key from the path.
        req: BookingRequest with ride_type, pickup/dropoff coords, idempotency_key.

    Returns:
        BookingResponse with the new provider_booking_id, CONFIRMED status,
        ride type, final price, and pickup ETA.

    Raises:
        HTTPException: 404 if provider unknown, 400 if ride type not offered.
    """
    _validate_provider(provider)

    # Confirm the ride type exists for this provider
    ride_type_config = next(
        (rt for rt in providers.get_ride_types(provider) if rt["ride_type"] == req.ride_type),
        None,
    )
    if ride_type_config is None:
        raise HTTPException(
            status_code=400,
            detail=f"Ride type '{req.ride_type}' not available for {provider}",
        )

    # Price at booking time — fluctuates like a real final price
    final_price = _fluctuate_price(ride_type_config["base_price"])
    pickup_eta = _fluctuate_eta(ride_type_config["base_pickup_eta"])

    record = store.create_booking(
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


@router.get("/{provider}/bookings/{booking_id}/status", response_model=RideStatusResponse)
def get_ride_status(provider: str, booking_id: str):
    """Get the current ride status for a booking.

    Status is computed from elapsed time since confirmation, so repeated calls
    naturally show the ride progressing toward completion.

    Args:
        provider: The provider key from the path.
        booking_id: The provider_booking_id to look up.

    Returns:
        RideStatusResponse with the current status and elapsed seconds.

    Raises:
        HTTPException: 404 if provider unknown or booking not found.
    """
    _validate_provider(provider)

    status = store.get_ride_status(booking_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    return RideStatusResponse(**status)


@router.post("/{provider}/bookings/{booking_id}/cancel", response_model=CancelResponse)
def cancel_booking(provider: str, booking_id: str):
    """Cancel a booking with the provider.

    Args:
        provider: The provider key from the path.
        booking_id: The provider_booking_id to cancel.

    Returns:
        CancelResponse with the booking ID and CANCELLED status.

    Raises:
        HTTPException: 404 if provider unknown or booking not found.
    """
    _validate_provider(provider)

    ok = store.cancel_booking(booking_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Booking not found")

    return CancelResponse(provider_booking_id=booking_id, status="CANCELLED")
