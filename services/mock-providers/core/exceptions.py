"""Domain exceptions for the Mock Providers service.

The service layer raises these framework-agnostic errors instead of HTTP
exceptions, keeping business logic decoupled from the web layer. The API layer
(api.provider_routes) catches them and maps each to the appropriate HTTP status.
"""

from __future__ import annotations


class ProviderServiceError(Exception):
    """Base class for all Mock Providers domain errors."""


class UnknownProviderError(ProviderServiceError):
    """Raised when a request targets a provider we do not simulate."""

    def __init__(self, provider: str) -> None:
        super().__init__(f"Unknown provider: {provider}")
        self.provider = provider


class RideTypeNotAvailableError(ProviderServiceError):
    """Raised when a booking names a ride type the provider does not offer."""

    def __init__(self, provider: str, ride_type: str) -> None:
        super().__init__(
            f"Ride type '{ride_type}' not available for {provider}"
        )
        self.provider = provider
        self.ride_type = ride_type


class BookingNotFoundError(ProviderServiceError):
    """Raised when a provider booking id does not resolve to a stored booking."""

    def __init__(self, provider_booking_id: str) -> None:
        super().__init__("Booking not found")
        self.provider_booking_id = provider_booking_id
