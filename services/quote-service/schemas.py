"""Request and response models for the Quote Service API.

These Pydantic models define the shapes that the AI Service (the only caller of
the Quote Service) sends and receives. ORM models in models.py remain the source
of truth for persisted state; these schemas are the HTTP boundary representation.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CreateSessionRequest(BaseModel):
    """Request body for POST /quotes/sessions.

    Pickup/dropoff coordinates are optional at creation time (a pickup may still
    be unresolved when the session is created) but are required before quotes can
    be fetched. Addresses are free-form labels for display.
    """

    user_id: str
    chat_session_id: uuid.UUID | None = None

    pickup_address: str | None = None
    pickup_lat: float | None = None
    pickup_lng: float | None = None

    dropoff_address: str | None = None
    dropoff_lat: float | None = None
    dropoff_lng: float | None = None


class SelectQuoteRequest(BaseModel):
    """Request body for POST /quotes/sessions/{id}/select.

    Records which specific quote the user chose. The quote must belong to the
    session being selected against.
    """

    quote_id: uuid.UUID


class QuoteOut(BaseModel):
    """A single stored quote as returned to the caller."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    ride_type: str
    price: float
    currency: str
    pickup_eta_minutes: int | None = None
    trip_duration_minutes: int | None = None
    available: bool


class QuoteSessionOut(BaseModel):
    """A quote session as returned to the caller."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: str
    chat_session_id: uuid.UUID | None = None

    pickup_address: str | None = None
    pickup_lat: float | None = None
    pickup_lng: float | None = None
    dropoff_address: str | None = None
    dropoff_lat: float | None = None
    dropoff_lng: float | None = None

    status: str
    created_at: datetime
    updated_at: datetime


class FetchQuotesResponse(BaseModel):
    """Response body for POST /quotes/sessions/{id}/fetch.

    Reports the session, the quotes fetched and stored, and any providers that
    could not be reached so the caller can surface partial results.
    """

    session: QuoteSessionOut
    quotes: list[QuoteOut] = Field(default_factory=list)
    unavailable_providers: list[str] = Field(default_factory=list)


class SelectQuoteResponse(BaseModel):
    """Response body for POST /quotes/sessions/{id}/select."""

    session: QuoteSessionOut
    selected_quote: QuoteOut


class SessionStateResponse(BaseModel):
    """Response body for GET /quotes/sessions/{id}.

    A read-only snapshot of a session's current state: the session fields
    (pickup/dropoff, status) plus the quotes currently stored for it. Used by the
    AI Service to build an authoritative ride-state summary for the model each
    turn, so it can reason over what is already known instead of re-asking.
    """

    session: QuoteSessionOut
    quotes: list[QuoteOut] = Field(default_factory=list)
