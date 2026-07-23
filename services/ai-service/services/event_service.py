"""Internal background-event handling for the AI Service.

The Quote and Booking services push background events to the AI Service over HTTP
(``POST /internal/events``) rather than to the frontend directly: the AI Service
is the single source of truth for what the user sees (design rule 11) and owns
the per-chat-session SSE streams. :class:`EventService` is the logic that:

  1. Turns a validated event envelope into the right server-pushed SSE events,
     routed to the session stream via the injected :class:`StreamPublisher`
     (keyed by ``(user_id, chat_session_id)`` — Requirement 7.2).
  2. Persists any spoken assistant message to chat history so the conversation
     record stays consistent.

All meaningfulness/mapping logic is delegated to the **pure** functions in
services.notifications; this class only performs the side effects. It performs no
quote/booking/ride state writes and never calls a provider, honoring the
tool-calling boundary (Requirement 9): the only persistence here is the AI
Service's own chat messages.

Event flows handled:

  QUOTE_DELTA (Requirements 3.3, 3.4, 3.5, 4.3)
    - ALWAYS push a ``quote_update`` (silent card refresh) — Requirement 3.3.
    - If the delta is meaningful, push an ``ai_notification`` and persist the
      deterministic spoken message — Requirement 3.4. Non-meaningful deltas push
      neither and never invoke the LLM — Requirement 3.5.

  Booking lifecycle events (Requirement 6.3)
    - Push a ``booking_update`` with the resulting status, and for key ride
      milestones push a spoken ``ride_status`` event and persist its message.

Documented limitation (selected-ride resolution): Requirement 4.3 calls for a
change to the *selected* ride's price to always be meaningful. The AI Service
does not persist which provider/ride_type the user selected, so the selected ride
is left unresolved here (``selected_ride=None``) and meaningfulness falls back to
cheapest/fastest rank changes among the changed items. The pure
:func:`decide_quote_notification` already accepts a selection, so this can be
tightened later.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from infra.stream_publisher import StreamPublisher
from models.chat_models import MessageRole
from repositories.chat_repository import ChatRepository
from schemas.event_schemas import BookingEventEnvelope, QuoteDeltaEvent
from services.notifications import (
    decide_booking_notification,
    decide_quote_notification,
)

logger = logging.getLogger("ai-service.events")


class EventService:
    """Routes validated background events into per-session SSE streams."""

    def __init__(self, publisher: StreamPublisher) -> None:
        """Wire the service to its stream publisher.

        Args:
            publisher: Used to push server-initiated SSE events to sessions.
        """
        self.publisher = publisher

    async def handle_quote_delta(
        self, event: QuoteDeltaEvent, db: AsyncSession
    ) -> None:
        """Route a QUOTE_DELTA into the session stream (Requirements 3.3, 3.4, 3.5).

        Always pushes a silent ``quote_update`` carrying the raw changes so the
        frontend can update its cards (Requirement 3.3). Then evaluates
        meaningfulness via the pure :func:`decide_quote_notification`; only when
        meaningful does it push an ``ai_notification`` and persist the
        deterministic spoken message (Requirement 3.4). Non-meaningful deltas stop
        after the silent update and never invoke the LLM (Requirement 3.5).

        A None ``chat_session_id`` is a no-op: there is no stream to route to.
        """
        if event.chat_session_id is None:
            # The originating quote session is not linked to a chat — nothing to show.
            return

        # Requirement 3.3 — always update the cards silently.
        await self.publisher.push_quote_update(
            event.user_id,
            event.chat_session_id,
            [change.model_dump() for change in event.changes],
        )

        # Requirements 3.4 / 3.5 — only meaningful changes interrupt and speak.
        # The selected ride is not resolved here (see module docstring), so we
        # pass no selection and rely on cheapest/fastest rank changes.
        decision = decide_quote_notification(event.changes, selected_ride=None)
        if not decision.meaningful or decision.message is None:
            return

        await self.publisher.push_ai_notification(
            event.user_id, event.chat_session_id, decision.message
        )
        await self._persist_spoken_message(
            db, event.chat_session_id, decision.message
        )

    async def handle_booking_event(
        self, event: BookingEventEnvelope, db: AsyncSession
    ) -> None:
        """Route a booking lifecycle event into the session stream (Requirement 6.3).

        Pushes a ``booking_update`` with the resulting status, and for key ride
        milestones additionally pushes a spoken ``ride_status`` event and persists
        its message.

        A None ``chat_session_id`` is a no-op: there is no stream to route to.
        """
        if event.chat_session_id is None:
            return

        booking_id = str(event.booking_id)
        decision = decide_booking_notification(event.event_type, event.status)

        if decision.booking_update:
            await self.publisher.push_booking_update(
                event.user_id, event.chat_session_id, booking_id, decision.status
            )

        if decision.spoken_message is not None:
            await self.publisher.push_ride_status(
                event.user_id, event.chat_session_id, booking_id, decision.spoken_message
            )
            await self._persist_spoken_message(
                db, event.chat_session_id, decision.spoken_message
            )

    async def _persist_spoken_message(
        self, db: AsyncSession, chat_session_id: uuid.UUID, message: str
    ) -> None:
        """Persist a spoken assistant message to chat history, best-effort.

        Keeping the spoken notification in the conversation record (as an
        assistant message) keeps history consistent with what the user heard.
        This is AI-owned chat state, not ride state, so it respects the
        tool-calling boundary (Requirement 9.1).

        The chat session is verified to exist first so a stale/unknown id does not
        raise a foreign-key error; any persistence failure is logged and swallowed
        so a history hiccup never prevents the live SSE push that already went out.
        """
        try:
            repo = ChatRepository(db)
            chat_session = await repo.get_chat_session(chat_session_id)
            if chat_session is None:
                logger.warning(
                    "Skipping spoken-message persistence: chat session %s not found",
                    chat_session_id,
                )
                return
            await repo.add_message(chat_session_id, MessageRole.ASSISTANT, message)
        except Exception:  # noqa: BLE001 - never fail the event on a history write
            logger.exception(
                "Failed to persist spoken message for chat session %s",
                chat_session_id,
            )
