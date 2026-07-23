"""Request and response models for the AI Service chat domain.

These Pydantic models define the HTTP boundary shapes the frontend sends and
receives, plus the in-process "conversation context" bundle used when handling
a message. ORM models in models.chat_models remain the source of truth for
persisted state; these schemas are the boundary representation.
"""

from __future__ import annotations

import uuid
from datetime import datetime

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
    """The context window loaded per request to drive a chat turn.

    Bundles the recent conversation history (last N stored messages, oldest
    first) with the current chat-linked ride state (quote session and booking
    ids). This is the bounded state replayed into a turn; authoritative quote
    and booking details still come from tools. Returned by
    ChatRepository.load_context.
    """

    chat_session_id: uuid.UUID
    user_id: str
    status: str
    quote_session_id: uuid.UUID | None = None
    booking_id: uuid.UUID | None = None
    messages: list[ChatMessageOut] = Field(default_factory=list)
