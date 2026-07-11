"""Domain exceptions for the Booking Service.

The service layer raises these framework-agnostic errors instead of HTTP
exceptions, keeping business logic decoupled from the web layer. The API layer
(api.booking_routes) catches them and maps each to the appropriate HTTP status.
"""

from __future__ import annotations


class BookingError(Exception):
    """Base class for all Booking Service domain errors."""


class BookingNotFoundError(BookingError):
    """Raised when a booking id does not resolve to a stored booking."""

    def __init__(self, booking_id: object) -> None:
        super().__init__(f"Booking {booking_id} not found")
        self.booking_id = booking_id


class ConfirmationNotExplicitError(BookingError):
    """Raised when a confirm request is not an explicit approval (confirmed != true)."""


class InvalidBookingStatusError(BookingError):
    """Raised when an action is not allowed for the booking's current status."""


class ConflictingBookingError(BookingError):
    """Raised when the user already has an active booking with the same provider."""


class MissingCoordinatesError(BookingError):
    """Raised when a provider action needs pickup/dropoff coordinates that are absent."""


class ProviderUnavailableError(BookingError):
    """Raised when an upstream provider call fails."""
