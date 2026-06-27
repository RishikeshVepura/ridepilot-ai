"""Request and response models for the AI Service chat domain.

These Pydantic models define the HTTP boundary shapes the frontend sends and
receives, plus the in-process "conversation context" bundle used when handling
a message. ORM models in models.py remain the source of truth for persisted
state; these schemas are the boundary representation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Location(BaseModel):
    """A geographic coordinate sent by the frontend (e.g. current GPS)."""

    lat: float
    lng: float


class ChatMessageRequest(BaseModel):
    """Request body for POST /api/chat/message.

    Mirrors the design API contract. ``chat_session_id`` is null on the user's
    first message; the AI Service creates a session and returns its id in the
    first SSE event. ``location`` is optional and carries the user's current
    coordinates when they choose to use their current location as pickup.
    """

    user_id: str
    chat_session_id: uuid.UUID | None = None
    message: str
    location: Location | None = None


class ChatMessageOut(BaseModel):
    """A single stored chat message as returned to the caller."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chat_session_id: uuid.UUID
    role: str
    content: str
    created_at: datetime


class ChatSessionOut(BaseModel):
    """A chat session as returned to the caller."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: str
    status: str
    quote_session_id: uuid.UUID | None = None
    booking_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class ConversationContext(BaseModel):
    """The context loaded per request to drive a chat turn.

    Bundles the recent conversation history (last N messages, oldest first) with
    the current ride state (linked quote session and booking) so the message
    handler and LLM have everything needed without re-querying. Returned by
    repository.load_context.
    """

    chat_session_id: uuid.UUID
    user_id: str
    status: str
    quote_session_id: uuid.UUID | None = None
    booking_id: uuid.UUID | None = None
    messages: list[ChatMessageOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal background-event envelopes (Quote/Booking Service -> AI Service).
#
# These define the validated shapes accepted at POST /internal/events. The
# bodies arrive over HTTP from other services and are treated as UNTRUSTED
# input: every field is validated here before the event consumer (events.py)
# acts on it. ORM/state remains owned by the producing services; the AI Service
# only routes these into per-session SSE streams and persists its own spoken
# chat messages.
# ---------------------------------------------------------------------------


class QuoteDeltaChange(BaseModel):
    """One changed (provider, ride_type) entry inside a QUOTE_DELTA event.

    Mirrors the change shape produced by the Quote Service monitoring worker
    (see quote-service/monitor.compute_changes). ``old_*`` fields are None for a
    newly-appeared option; ``new_*`` fields are None for an option that
    disappeared. Prices/ETAs are otherwise the rounded current values.
    """

    provider: str
    ride_type: str
    old_price: float | None = None
    new_price: float | None = None
    old_pickup_eta_minutes: int | None = None
    new_pickup_eta_minutes: int | None = None


class QuoteDeltaEvent(BaseModel):
    """Envelope for a QUOTE_DELTA event posted to /internal/events.

    Published by the Quote Service monitoring worker when a session's quotes
    change (Requirement 3.2). ``chat_session_id`` may be None when the originating
    quote session is not linked to a chat session, in which case there is no SSE
    stream to route to and the event is a no-op. ``quote_session_id`` is accepted
    as a free-form string since it is not used for routing.
    """

    event_type: Literal["QUOTE_DELTA"]
    user_id: str
    chat_session_id: uuid.UUID | None = None
    quote_session_id: str | None = None
    changes: list[QuoteDeltaChange] = Field(default_factory=list)


class BookingEventEnvelope(BaseModel):
    """Envelope for a booking lifecycle event posted to /internal/events.

    Covers the Booking Service milestones (DRIVER_ASSIGNED, DRIVER_ARRIVING,
    RIDE_STARTED, RIDE_COMPLETED, cancellation, etc. — see booking-service
    BookingEventType) so the AI Service can push ``booking_update`` and spoken
    ``ride_status`` events (Requirement 6.3). ``chat_session_id`` may be None when
    the booking is not linked to a chat session, in which case the event is a
    no-op. ``status`` is optional; when omitted the consumer derives it from the
    event type. ``payload`` carries any extra producer detail and is not
    interpreted here.

    NOTE: the Booking Service does not yet POST these events; this schema defines
    the contract the consumer handles when that producer is wired up.
    """

    event_type: str
    user_id: str
    chat_session_id: uuid.UUID | None = None
    booking_id: uuid.UUID
    status: str | None = None
    payload: dict[str, Any] | None = None
