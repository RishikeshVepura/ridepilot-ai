"""Provider adapter layer for the Quote Service.

All provider-specific logic is isolated behind these adapters (design rule 6).
The rest of the Quote Service works only with the normalized shape defined here
and never talks to a provider's HTTP API directly.

Each adapter knows how to:
  - call its provider over HTTP (via the Mock Providers service), and
  - normalize the provider's response into a common ``NormalizedQuote`` shape.

Factory helpers (``get_adapter`` / ``get_all_adapters``) return adapters by
provider name so callers can fan out across every supported provider without
hard-coding provider details.

Requirements: 2.2 (normalize quotes into a common shape), 9.2 (provider calls
are isolated behind an adapter; callers never hit provider APIs directly).
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("quote-service.provider")

# Providers we simulate, in a stable order. Matches the Mock Providers config
# (services/mock-providers/providers.py). Used by get_all_adapters so callers
# can fetch from every provider in parallel.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("uber", "lyft", "waymo")

# Base URL of the Mock Providers service. Inside the Docker network this resolves
# to the compose service name; overridable via env for other environments.
MOCK_PROVIDERS_URL = os.getenv("MOCK_PROVIDERS_URL", "http://mock-providers:8004")

# Default per-request timeout (seconds) for provider HTTP calls.
DEFAULT_TIMEOUT_SECONDS = 10.0


class ProviderError(Exception):
    """Raised when a provider call fails or returns an unusable response.

    Carries the provider key so callers (e.g. the fetch endpoint and the
    monitoring worker) can record a PROVIDER_UNAVAILABLE event and continue
    with the other providers instead of failing the whole fetch.

    Attributes:
        provider: The provider key this error relates to.
        message: A human-readable description of what went wrong.
    """

    def __init__(self, provider: str, message: str):
        self.provider = provider
        self.message = message
        super().__init__(f"[{provider}] {message}")


@dataclass(frozen=True)
class NormalizedQuote:
    """A single quote normalized into the Quote Service's common shape.

    This is the provider-agnostic representation every adapter produces. Its
    fields line up with the columns on the ``quotes`` table so callers can
    persist a quote without any further provider-specific mapping.

    Fields:
        provider: The provider key this quote came from (e.g. "uber").
        ride_type: The provider's ride type name (e.g. "UberX").
        price: Quoted price.
        currency: ISO currency code. Defaults to "USD".
        pickup_eta_minutes: Estimated minutes until pickup, if provided.
        trip_duration_minutes: Estimated trip length in minutes, if provided.
        available: Whether this option is currently bookable.
    """

    provider: str
    ride_type: str
    price: float
    currency: str = "USD"
    pickup_eta_minutes: int | None = None
    trip_duration_minutes: int | None = None
    available: bool = True


class ProviderAdapter(ABC):
    """Common interface every provider adapter implements.

    Subclasses encapsulate how to reach a specific provider and how to turn its
    raw response into ``NormalizedQuote`` objects. Callers depend only on this
    interface, keeping provider details out of the rest of the service.
    """

    #: The provider key this adapter serves (set by the subclass instance).
    provider: str

    @abstractmethod
    async def fetch_quotes(
        self,
        *,
        pickup_lat: float,
        pickup_lng: float,
        dropoff_lat: float,
        dropoff_lng: float,
    ) -> list[NormalizedQuote]:
        """Fetch current quotes for a pickup/dropoff and normalize them.

        Args:
            pickup_lat: Pickup latitude.
            pickup_lng: Pickup longitude.
            dropoff_lat: Dropoff latitude.
            dropoff_lng: Dropoff longitude.

        Returns:
            A list of NormalizedQuote, one per available ride type.

        Raises:
            ProviderError: If the provider call fails or returns an
                unusable/malformed response.
        """
        raise NotImplementedError


@dataclass
class MockProviderAdapter(ProviderAdapter):
    """Adapter that fetches quotes from the Mock Providers service over HTTP.

    Calls ``GET {base_url}/{provider}/quotes`` and normalizes the response into
    ``NormalizedQuote`` objects. A pre-built ``httpx.AsyncClient`` may be
    injected (e.g. a shared, reusable client); otherwise one is created per
    request and closed afterwards.

    Args:
        provider: The provider key this adapter serves (must be supported).
        base_url: Base URL of the Mock Providers service.
        client: Optional shared httpx.AsyncClient. When omitted, a short-lived
            client is created per fetch.
        timeout: Per-request timeout in seconds (used when no client is given).
    """

    provider: str
    base_url: str = MOCK_PROVIDERS_URL
    client: httpx.AsyncClient | None = field(default=None, repr=False)
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    async def fetch_quotes(
        self,
        *,
        pickup_lat: float,
        pickup_lng: float,
        dropoff_lat: float,
        dropoff_lng: float,
    ) -> list[NormalizedQuote]:
        """Fetch and normalize quotes for this provider. See base class."""
        url = f"{self.base_url.rstrip('/')}/{self.provider}/quotes"
        params = {
            "pickup_lat": pickup_lat,
            "pickup_lng": pickup_lng,
            "dropoff_lat": dropoff_lat,
            "dropoff_lng": dropoff_lng,
        }

        logger.info("→ %s GET %s", self.provider, url)
        try:
            if self.client is not None:
                response = await self.client.get(url, params=params)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "✗ %s quotes — HTTP %s", self.provider, exc.response.status_code
            )
            raise ProviderError(
                self.provider,
                f"quotes request returned {exc.response.status_code}",
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("✗ %s quotes — request failed: %s", self.provider, exc)
            raise ProviderError(
                self.provider, f"quotes request failed: {exc}"
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                self.provider, "quotes response was not valid JSON"
            ) from exc

        normalized = self._normalize_quotes(body)
        logger.info(
            "← %s %d quote option(s)", self.provider, len(normalized)
        )
        return normalized

    def _normalize_quotes(self, body: dict) -> list[NormalizedQuote]:
        """Map a raw quotes response body into NormalizedQuote objects.

        Args:
            body: The decoded JSON body from GET /{provider}/quotes. Expected to
                contain a "quotes" list of ride options.

        Returns:
            A list of NormalizedQuote in the order the provider returned them.

        Raises:
            ProviderError: If "quotes" is missing/not a list, or any option is
                missing required fields.
        """
        options = body.get("quotes")
        if not isinstance(options, list):
            raise ProviderError(
                self.provider, "quotes response missing a 'quotes' list"
            )

        normalized: list[NormalizedQuote] = []
        for option in options:
            if not isinstance(option, dict):
                raise ProviderError(
                    self.provider, "quote option was not an object"
                )
            try:
                normalized.append(
                    NormalizedQuote(
                        provider=self.provider,
                        ride_type=option["ride_type"],
                        price=float(option["price"]),
                        currency=option.get("currency", "USD"),
                        pickup_eta_minutes=option.get("pickup_eta_minutes"),
                        trip_duration_minutes=option.get("trip_duration_minutes"),
                        available=option.get("available", True),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError(
                    self.provider, f"malformed quote option: {exc}"
                ) from exc

        return normalized


def get_adapter(
    provider: str,
    *,
    base_url: str = MOCK_PROVIDERS_URL,
    client: httpx.AsyncClient | None = None,
) -> ProviderAdapter:
    """Return an adapter for a single supported provider.

    Args:
        provider: The provider key (e.g. "uber", "lyft", "waymo").
        base_url: Base URL of the Mock Providers service.
        client: Optional shared httpx.AsyncClient to reuse across calls.

    Returns:
        A ProviderAdapter for the requested provider.

    Raises:
        ValueError: If the provider is not supported.
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported provider '{provider}'. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS)}"
        )
    return MockProviderAdapter(provider=provider, base_url=base_url, client=client)


def get_all_adapters(
    *,
    base_url: str = MOCK_PROVIDERS_URL,
    client: httpx.AsyncClient | None = None,
) -> list[ProviderAdapter]:
    """Return adapters for every supported provider.

    Useful for fanning out a quote fetch across all providers in parallel.

    Args:
        base_url: Base URL of the Mock Providers service.
        client: Optional shared httpx.AsyncClient to reuse across all adapters.

    Returns:
        A list of ProviderAdapter, one per supported provider, in
        SUPPORTED_PROVIDERS order.
    """
    return [
        get_adapter(provider, base_url=base_url, client=client)
        for provider in SUPPORTED_PROVIDERS
    ]
