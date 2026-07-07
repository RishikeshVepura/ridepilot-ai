"""Internal background-event consumer for the AI Service (task 6.4).

The Quote and Booking services push background events to the AI Service over HTTP
(``POST /internal/events``) rather than to the frontend directly: the AI Service
is the single source of truth for what the user sees (design rule 11) and owns
the per-chat-session SSE streams. This module is that consumer — the thin I/O
layer that:

  1. Accepts and validates the untrusted event envelope (Pydantic schemas in
     schemas.py), dispatching on ``event_type``.
  2. Routes the resulting server-pushed events to the right SSE stream via the
     per-session :data:`event_bus`, keyed by ``(user_id, chat_session_id)``
     (Requirement 7.2 — one stream per chat session).
  3. Persists any spoken assistant message to chat history so the conversation
     record stays consistent.

All meaningfulness/mapping logic is delegated to the **pure** functions in
notifications.py; this module only performs the side effects. It deliberately
performs no quote/booking/ride state writes and never calls a provider, honoring
the tool-calling boundary (Requirement 9): the only persistence here is the AI
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
    - NOTE: wiring the Booking Service to actually POST these events is out of
      scope for this task; the consumer is ready for them.

Documented limitation (selected-ride resolution): Requirement 4.3 calls for a
change to the *selected* ride's price to always be meaningful. The AI Service
does not persist which provider/ride_type the user selected (only the linked
quote_session_id / booking_id on the chat session), and there is no
get-all-quotes tool to resolve it without crossing the service boundary. Rather
than add a provider/DB bypass, the selected ride is left unresolved here
(``selected_ride=None``) and meaningfulness falls back to cheapest/fastest rank
changes among the changed items. The pure :func:`decide_quote_notification`
already accepts a :class:`SelectedRide`, so this can be tightened later by
resolving the selection through the Quote Service.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import repository
from db import get_session
from event_bus import event_bus, make_session_key
from models import MessageRole
from notifications import (
    BOOKING_EVENT_TYPES,
    QUOTE_DELTA_EVENT_TYPE,
    decide_booking_notification,
    decide_quote_notification,
)
from schemas import BookingEventEnvelope, QuoteDeltaEvent

logger = logging.getLogger("ai-service.events")

router = APIRouter(tags=["internal-events"])


async def push_quote_snapshot(
    user_id: str, chat_session_id: uuid.UUID | None, quotes: list
) -> None:
    """Push freshly fetched quotes to a session's SSE stream as a quote_snapshot.

    Called right after ``fetch_quotes`` succeeds (live LLM and stub paths) so the
    frontend's ride panel appears immediately — before the assistant finishes
    composing its spoken summary — instead of staying empty until the first
    monitoring delta. A None chat_session_id (or empty quotes) is a no-op.

    Args:
        user_id: The owning user (stream routing key).
        chat_session_id: The chat session whose stream to push to.
        quotes: The normalized quote dicts from the fetch_quotes result.
    """
    if chat_session_id is None or not quotes:
        return
    key = make_session_key(user_id, chat_session_id)
    await event_bus.publish(key, {"type": "quote_snapshot", "quotes": quotes})


async def push_route_map(
    user_id: str, chat_session_id: uuid.UUID | None, session: dict | None
) -> None:
    """Push the pickup/dropoff coordinates to a session's SSE stream (route_map).

    Called right after ``create_quote_session`` succeeds (live LLM and stub
    paths) so the frontend can render a map with both points as soon as the
    search starts. The coordinates come from the created quote session — which,
    until geocoding exists, are the fixed test coordinates the tool layer
    backfills — paired with the free-form address labels for display.

    A None chat_session_id, a missing session payload, or missing coordinates is
    a no-op (nothing to plot).

    Args:
        user_id: The owning user (stream routing key).
        chat_session_id: The chat session whose stream to push to.
        session: The created QuoteSessionOut dict (carries coords + addresses).
    """
    if chat_session_id is None or not isinstance(session, dict):
        return

    pickup_lat = session.get("pickup_lat")
    pickup_lng = session.get("pickup_lng")
    dropoff_lat = session.get("dropoff_lat")
    dropoff_lng = session.get("dropoff_lng")
    if None in (pickup_lat, pickup_lng, dropoff_lat, dropoff_lng):
        return

    key = make_session_key(user_id, chat_session_id)
    await event_bus.publish(
        key,
        {
            "type": "route_map",
            "pickup": {
                "lat": float(pickup_lat),
                "lng": float(pickup_lng),
                "label": session.get("pickup_address") or "Pickup",
            },
            "dropoff": {
                "lat": float(dropoff_lat),
                "lng": float(dropoff_lng),
                "label": session.get("dropoff_address") or "Dropoff",
            },
        },
    )


async def _persist_spoken_message(
    db: AsyncSession, chat_session_id: uuid.UUID, message: str
) -> None:
    """Persist a spoken assistant message to chat history, best-effort.

    Keeping the spoken notification in the conversation record (as an assistant
    message) keeps history consistent with what the user heard. This is AI-owned
    chat state, not ride state, so it respects the tool-calling boundary
    (Requirement 9.1).

    The chat session is verified to exist first so a stale/unknown id does not
    raise a foreign-key error; any persistence failure is logged and swallowed so
    a history hiccup never prevents the live SSE push that already went out.

    Args:
        db: Active database session.
        chat_session_id: The chat session the message belongs to.
        message: The spoken message text to record.
    """
    try:
        chat_session = await repository.get_chat_session(db, chat_session_id)
        if chat_session is None:
            logger.warning(
                "Skipping spoken-message persistence: chat session %s not found",
                chat_session_id,
            )
            return
        await repository.add_message(
            db, chat_session_id, MessageRole.ASSISTANT, message
        )
    except Exception:  # noqa: BLE001 - never fail the event on a history write
        logger.exception(
            "Failed to persist spoken message for chat session %s", chat_session_id
        )


async def handle_quote_delta(event: QuoteDeltaEvent, db: AsyncSession) -> None:
    """Route a QUOTE_DELTA into the session stream (Requirements 3.3, 3.4, 3.5).

    Always pushes a silent ``quote_update`` carrying the raw changes so the
    frontend can update its cards (Requirement 3.3). Then evaluates meaningfulness
    via the pure :func:`decide_quote_notification`; only when meaningful does it
    push an ``ai_notification`` and persist the deterministic spoken message
    (Requirement 3.4). Non-meaningful deltas stop after the silent update and
    never invoke the LLM (Requirement 3.5).

    A None ``chat_session_id`` is a no-op: there is no stream to route to.

    Args:
        event: The validated QUOTE_DELTA envelope.
        db: Active database session (for persisting a spoken message).
    """
    if event.chat_session_id is None:
        # The originating quote session is not linked to a chat — nothing to show.
        return

    key = make_session_key(event.user_id, event.chat_session_id)

    # Requirement 3.3 — always update the cards silently.
    await event_bus.publish(
        key,
        {
            "type": "quote_update",
            "changes": [change.model_dump() for change in event.changes],
        },
    )

    # Requirements 3.4 / 3.5 — only meaningful changes interrupt and speak. The
    # selected ride is not resolved here (see module docstring limitation), so we
    # pass no selection and rely on cheapest/fastest rank changes.
    decision = decide_quote_notification(event.changes, selected_ride=None)
    if not decision.meaningful or decision.message is None:
        return

    await event_bus.publish(
        key, {"type": "ai_notification", "message": decision.message}
    )
    await _persist_spoken_message(db, event.chat_session_id, decision.message)


async def handle_booking_event(
    event: BookingEventEnvelope, db: AsyncSession
) -> None:
    """Route a booking lifecycle event into the session stream (Requirement 6.3).

    Pushes a ``booking_update`` with the resulting status, and for key ride
    milestones (driver assigned/arriving, ride started/completed, cancellation)
    additionally pushes a spoken ``ride_status`` event and persists its message.

    A None ``chat_session_id`` is a no-op: there is no stream to route to.

    Args:
        event: The validated booking event envelope.
        db: Active database session (for persisting a spoken message).
    """
    if event.chat_session_id is None:
        return

    key = make_session_key(event.user_id, event.chat_session_id)
    booking_id = str(event.booking_id)

    decision = decide_booking_notification(event.event_type, event.status)

    if decision.booking_update:
        await event_bus.publish(
            key,
            {
                "type": "booking_update",
                "booking_id": booking_id,
                "status": decision.status,
            },
        )

    if decision.spoken_message is not None:
        await event_bus.publish(
            key,
            {
                "type": "ride_status",
                "booking_id": booking_id,
                "message": decision.spoken_message,
            },
        )
        await _persist_spoken_message(
            db, event.chat_session_id, decision.spoken_message
        )


@router.post("/internal/events")
async def receive_internal_event(
    request: Request, db: AsyncSession = Depends(get_session)
) -> dict:
    """Receive a background event from the Quote/Booking services and route it.

    Dispatches on ``event_type``: QUOTE_DELTA events are handled by
    :func:`handle_quote_delta` and recognized booking events by
    :func:`handle_booking_event`. The body is untrusted and validated against the
    matching Pydantic envelope before any action is taken; a malformed body for a
    known type is rejected with 422. Unknown/unsupported event types are accepted
    but ignored so a future producer cannot break this endpoint. Returns promptly
    with a small acknowledgement in all cases.

    Args:
        request: The incoming request whose JSON body is the event envelope.
        db: Active database session.

    Returns:
        ``{"status": "accepted"}`` when an event was handled, or
        ``{"status": "ignored", ...}`` for an unsupported/unroutable event.

    Raises:
        HTTPException: 400 if the body is not valid JSON object, 422 if a known
            event type fails validation.
    """
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - normalize any parse failure
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422, detail="event body must be a JSON object"
        )

    event_type = payload.get("event_type")

    if event_type == QUOTE_DELTA_EVENT_TYPE:
        try:
            quote_event = QuoteDeltaEvent.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        await handle_quote_delta(quote_event, db)
        return {"status": "accepted"}

    if event_type in BOOKING_EVENT_TYPES:
        try:
            booking_event = BookingEventEnvelope.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        await handle_booking_event(booking_event, db)
        return {"status": "accepted"}

    # Unknown or unsupported event type — accept without acting so producers are
    # never blocked by an event this consumer does not (yet) understand.
    logger.info("Ignoring unsupported internal event_type=%r", event_type)
    return {"status": "ignored", "reason": "unsupported event_type"}
