"""Provider client layer for the Booking Service.

All provider-specific HTTP calls are isolated behind this module (design rule
6). The booking endpoints work only with the normalized results returned here
and never talk to a provider's HTTP API directly.

The Booking Service talks to the Mock Providers service for four things:
  - re-verifying the final price for a selected ride type (via GET /{provider}/quotes),
  - confirming a booking (via POST /{provider}/bookings, idempotent),
  - fetching ride status after confirmation (via GET /{provider}/bookings/{id}/status), and
  - cancelling a confirmed booking (via POST /{provider}/bookings/{id}/cancel).

Requirements: 5.2 (re-verify final price), 5.4 (confirm with provider),
5.5 (idempotency key prevents duplicates), 6.1/6.2 (track ride status after
confirmation), 6.4 (cancel with provider), 9.2 (provider calls isolated behind a
client; callers never hit provider APIs directly).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

# Base URL of the Mock Providers service. Inside the Docker network this resolves
# to the compose service name; overridable via env for other environments.
MOCK_PROVIDERS_URL = os.getenv("MOCK_PROVIDERS_URL", "http://mock-providers:8004")

# Default per-request timeout (seconds) for provider HTTP calls.
DEFAULT_TIMEOUT_SECONDS = 10.0


class ProviderError(Exception):
    """Raised when a provider call fails or returns an unusable response.

    Carries the provider key so callers (the booking endpoints) can surface a
    meaningful error and avoid leaving a booking in an inconsistent state.

    Attributes:
        provider: The provider key this error relates to.
        message: A human-readable description of what went wrong.
    """

    def __init__(self, provider: str, message: str):
        self.provider = provider
        self.message = message
        super().__init__(f"[{provider}] {message}")


@dataclass(frozen=True)
class VerifiedPrice:
    """The re-verified price for a ride type, normalized for the caller.

    Fields:
        provider: The provider key the price came from.
        ride_type: The ride type the price applies to.
        price: The current (re-verified) price.
        currency: ISO currency code.
        pickup_eta_minutes: Current pickup ETA in minutes, if provided.
    """

    provider: str
    ride_type: str
    price: float
    currency: str = "USD"
    pickup_eta_minutes: int | None = None


@dataclass(frozen=True)
class RideStatus:
    """The current ride status for a confirmed booking, normalized for callers.

    Fields:
        provider: The provider key the status came from.
        provider_booking_id: The provider's booking id this status refers to.
        status: The provider's ride stage (e.g. "DRIVER_ASSIGNED",
            "DRIVER_ARRIVING", "RIDE_STARTED", "RIDE_COMPLETED", "CANCELLED").
        elapsed_seconds: Seconds since confirmation, if reported by the provider.
    """

    provider: str
    provider_booking_id: str
    status: str
    elapsed_seconds: int | None = None


@dataclass(frozen=True)
class ProviderBooking:
    """The result of confirming a booking with a provider.

    Fields:
        provider: The provider key that confirmed the booking.
        provider_booking_id: The provider's ID for the new booking.
        status: Provider booking status ("CONFIRMED" on success).
        ride_type: The confirmed ride type.
        final_price: The final confirmed price.
        currency: ISO currency code.
        pickup_eta_minutes: Pickup ETA at confirmation time.
    """

    provider: str
    provider_booking_id: str
    status: str
    ride_type: str
    final_price: float
    currency: str = "USD"
    pickup_eta_minutes: int | None = None


def _base_url() -> str:
    """Return the Mock Providers base URL without a trailing slash."""
    return MOCK_PROVIDERS_URL.rstrip("/")


async def verify_price(
    *,
    provider: str,
    ride_type: str,
    pickup_lat: float,
    pickup_lng: float,
    dropoff_lat: float,
    dropoff_lng: float,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> VerifiedPrice:
    """Re-verify the current price for a ride type by re-quoting the provider.

    The Mock Providers service has no dedicated "verify" endpoint, so the final
    price is re-verified by re-fetching the provider's quotes and reading the
    current price for the selected ride type. Provider prices fluctuate between
    fetches, so this naturally surfaces price changes vs. the originally selected
    price. (Requirement 5.2)

    Args:
        provider: The provider key (e.g. "uber").
        ride_type: The ride type to re-verify (e.g. "UberX").
        pickup_lat: Pickup latitude.
        pickup_lng: Pickup longitude.
        dropoff_lat: Dropoff latitude.
        dropoff_lng: Dropoff longitude.
        client: Optional shared httpx.AsyncClient to reuse.
        timeout: Per-request timeout in seconds (used when no client is given).

    Returns:
        A VerifiedPrice carrying the current price for the ride type.

    Raises:
        ProviderError: If the provider call fails, returns an unusable response,
            or no longer offers the requested ride type.
    """
    url = f"{_base_url()}/{provider}/quotes"
    params = {
        "pickup_lat": pickup_lat,
        "pickup_lng": pickup_lng,
        "dropoff_lat": dropoff_lat,
        "dropoff_lng": dropoff_lng,
    }

    body = await _get_json(provider, url, params=params, client=client, timeout=timeout)

    options = body.get("quotes")
    if not isinstance(options, list):
        raise ProviderError(provider, "quotes response missing a 'quotes' list")

    for option in options:
        if isinstance(option, dict) and option.get("ride_type") == ride_type:
            try:
                return VerifiedPrice(
                    provider=provider,
                    ride_type=ride_type,
                    price=float(option["price"]),
                    currency=option.get("currency", "USD"),
                    pickup_eta_minutes=option.get("pickup_eta_minutes"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError(
                    provider, f"malformed quote option: {exc}"
                ) from exc

    raise ProviderError(
        provider, f"ride type '{ride_type}' is no longer offered"
    )


async def confirm_booking(
    *,
    provider: str,
    ride_type: str,
    idempotency_key: str,
    pickup_lat: float,
    pickup_lng: float,
    dropoff_lat: float,
    dropoff_lng: float,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ProviderBooking:
    """Confirm a booking with the provider, idempotent on idempotency_key.

    Calls POST /{provider}/bookings. The Mock Providers service dedupes on the
    idempotency_key, so retrying with the same key returns the original booking
    instead of creating a duplicate. (Requirements 5.4, 5.5)

    Args:
        provider: The provider key (e.g. "uber").
        ride_type: The ride type to confirm.
        idempotency_key: Stable key used to dedupe retried confirmations.
        pickup_lat: Pickup latitude.
        pickup_lng: Pickup longitude.
        dropoff_lat: Dropoff latitude.
        dropoff_lng: Dropoff longitude.
        client: Optional shared httpx.AsyncClient to reuse.
        timeout: Per-request timeout in seconds (used when no client is given).

    Returns:
        A ProviderBooking with the provider_booking_id and final price.

    Raises:
        ProviderError: If the provider call fails or returns an unusable response.
    """
    url = f"{_base_url()}/{provider}/bookings"
    payload = {
        "ride_type": ride_type,
        "pickup_lat": pickup_lat,
        "pickup_lng": pickup_lng,
        "dropoff_lat": dropoff_lat,
        "dropoff_lng": dropoff_lng,
        "idempotency_key": idempotency_key,
    }

    body = await _post_json(provider, url, json=payload, client=client, timeout=timeout)

    try:
        return ProviderBooking(
            provider=provider,
            provider_booking_id=body["provider_booking_id"],
            status=body.get("status", "CONFIRMED"),
            ride_type=body.get("ride_type", ride_type),
            final_price=float(body["final_price"]),
            currency=body.get("currency", "USD"),
            pickup_eta_minutes=body.get("pickup_eta_minutes"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderError(provider, f"malformed booking response: {exc}") from exc


async def get_ride_status(
    *,
    provider: str,
    provider_booking_id: str,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> RideStatus:
    """Fetch the current ride status for a confirmed booking from the provider.

    Calls GET /{provider}/bookings/{id}/status. The mock provider derives the
    status from elapsed time since confirmation, so repeated calls naturally show
    the ride progressing. This is the only place ride status is fetched from a
    provider; the ride tracker works exclusively with the normalized RideStatus
    returned here (design rule 6 / Requirement 9.2). (Requirements 6.1, 6.2)

    Args:
        provider: The provider key (e.g. "uber").
        provider_booking_id: The provider's booking id to look up.
        client: Optional shared httpx.AsyncClient to reuse.
        timeout: Per-request timeout in seconds (used when no client is given).

    Returns:
        A RideStatus carrying the current ride stage and elapsed seconds.

    Raises:
        ProviderError: If the provider call fails or returns an unusable response.
    """
    url = f"{_base_url()}/{provider}/bookings/{provider_booking_id}/status"
    body = await _get_json(provider, url, client=client, timeout=timeout)

    try:
        return RideStatus(
            provider=provider,
            provider_booking_id=body.get("provider_booking_id", provider_booking_id),
            status=body["status"],
            elapsed_seconds=body.get("elapsed_seconds"),
        )
    except (KeyError, TypeError) as exc:
        raise ProviderError(
            provider, f"malformed ride status response: {exc}"
        ) from exc


async def cancel_booking(
    *,
    provider: str,
    provider_booking_id: str,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Cancel a confirmed booking with the provider.

    Calls POST /{provider}/bookings/{id}/cancel. (Requirement 6.4)

    Args:
        provider: The provider key (e.g. "uber").
        provider_booking_id: The provider's booking id to cancel.
        client: Optional shared httpx.AsyncClient to reuse.
        timeout: Per-request timeout in seconds (used when no client is given).

    Returns:
        The provider's reported status string ("CANCELLED" on success).

    Raises:
        ProviderError: If the provider call fails or returns an unusable response.
    """
    url = f"{_base_url()}/{provider}/bookings/{provider_booking_id}/cancel"
    body = await _post_json(provider, url, json=None, client=client, timeout=timeout)
    return body.get("status", "CANCELLED")


async def _get_json(
    provider: str,
    url: str,
    *,
    params: dict | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Issue a GET and return the decoded JSON body, raising ProviderError on failure."""
    try:
        if client is not None:
            response = await client.get(url, params=params)
        else:
            async with httpx.AsyncClient(timeout=timeout) as c:
                response = await c.get(url, params=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ProviderError(
            provider, f"request returned {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderError(provider, f"request failed: {exc}") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(provider, "response was not valid JSON") from exc


async def _post_json(
    provider: str,
    url: str,
    *,
    json: dict | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Issue a POST and return the decoded JSON body, raising ProviderError on failure."""
    try:
        if client is not None:
            response = await client.post(url, json=json)
        else:
            async with httpx.AsyncClient(timeout=timeout) as c:
                response = await c.post(url, json=json)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ProviderError(
            provider, f"request returned {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderError(provider, f"request failed: {exc}") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(provider, "response was not valid JSON") from exc
