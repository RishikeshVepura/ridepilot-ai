"""Domain exceptions for the Quote Service.

The service layer raises these framework-agnostic errors instead of HTTP
exceptions, keeping business logic decoupled from the web layer. The API layer
(api.quote_routes) catches them and maps each to the appropriate HTTP status.
"""

from __future__ import annotations


class QuoteError(Exception):
    """Base class for all Quote Service domain errors."""


class QuoteSessionNotFoundError(QuoteError):
    """Raised when a session id does not resolve to a stored quote session."""

    def __init__(self, session_id: object) -> None:
        super().__init__(f"Quote session {session_id} not found")
        self.session_id = session_id


class QuoteNotFoundError(QuoteError):
    """Raised when a quote id does not resolve to a stored quote."""

    def __init__(self, quote_id: object) -> None:
        super().__init__(f"Quote {quote_id} not found")
        self.quote_id = quote_id


class SessionCancelledError(QuoteError):
    """Raised when an action is attempted on a cancelled session."""


class MissingCoordinatesError(QuoteError):
    """Raised when quotes are fetched before pickup/dropoff coordinates are set."""


class QuoteNotInSessionError(QuoteError):
    """Raised when a selected quote does not belong to the target session."""
