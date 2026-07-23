"""Public chat + streaming endpoints for the AI Service.

These are the only endpoints the frontend talks to (design rule 1). Together
they implement the chat session lifecycle from design section 7:

    POST /api/chat/message
        The user sends a message. The handler resolves (or creates, on the first
        message) the chat session, persists the user message, loads conversation
        context, and streams the assistant's reply back as SSE — token by token,
        ending with a ``done`` event. A brand-new conversation leads with a
        ``session_created`` event carrying the new chat_session_id.

    GET /api/stream/{user_id}/{chat_session_id}
        A long-lived SSE stream of all server-pushed events for one chat session
        (Requirement 7.2). Subscribes to the per-session event bus and relays
        events as they are published, with periodic keepalive comments.

    POST /api/sessions/{user_id}/{chat_session_id}/stop
        Stop quote monitoring for a chat session at the UI's explicit request.

The reply to a user's message is streamed on the POST response only; the GET
stream carries only server-initiated updates (background ``quote_update`` /
``booking_update`` / ``ride_status`` and proactive ``ai_notification`` events).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_chat_responder, get_session
from db.database import database
from infra.event_bus import SessionKey, event_bus, make_session_key
from models.chat_models import MessageRole
from repositories.chat_repository import ChatRepository
from schemas.chat_schemas import ChatMessageRequest
from services.responder import ChatResponder
from tools import tools
from tools.tools import ToolContext

logger = logging.getLogger("ai-service.routes")

router = APIRouter(tags=["chat"])

# How often (seconds) to emit an SSE keepalive comment on an idle stream.
SSE_KEEPALIVE_INTERVAL_SECONDS = 15.0

# Grace period before stopping monitoring for a disconnected stream, giving a
# transient EventSource reconnect time to re-subscribe.
MONITORING_STOP_GRACE_SECONDS = 5.0

# Standard SSE response headers. Disable caching and proxy buffering so events
# are delivered immediately, and keep the connection alive for the stream's life.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def format_sse(event: dict) -> str:
    """Serialize an event dict as a single SSE ``data:`` frame.

    Produces ``data: <json>\\n\\n`` — the JSON payload on one data line followed
    by the blank line that terminates the event.
    """
    return f"data: {json.dumps(event)}\n\n"


def _keepalive_comment() -> str:
    """Return an SSE comment frame used to keep an idle connection open."""
    return ": keepalive\n\n"


@router.post("/api/chat/message")
async def post_chat_message(
    req: ChatMessageRequest,
    db: AsyncSession = Depends(get_session),
    responder: ChatResponder = Depends(get_chat_responder),
) -> StreamingResponse:
    """Handle a user message and stream the assistant's reply back as SSE.

    Lifecycle: resolve/create the chat session, persist the user message, load
    conversation context, then return a ``text/event-stream`` response that emits
    ``session_created`` (new sessions only), streams the reply as ``token``
    events, and ends with ``done``. The full assistant message is persisted once
    streaming completes.

    Raises:
        HTTPException: 404 if a chat_session_id is supplied but no such session
            exists.
    """
    # Steps 1-3 run before we start streaming so input errors (e.g. an unknown
    # session id) surface as a normal HTTP error rather than mid-stream.
    is_new_session = req.chat_session_id is None
    repo = ChatRepository(db)
    try:
        chat_session = await repo.get_or_create_session(
            req.user_id, req.chat_session_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Load context BEFORE saving the user message so the history replay doesn't
    # include the current turn's message (it is appended separately by the
    # responder/LLM). Saving first then loading caused the user message to appear
    # twice in the prompt.
    context = await repo.load_context(chat_session.id)

    await repo.add_message(chat_session.id, MessageRole.USER, req.message)

    # Build the trusted tool context for this turn: identity (never model-
    # supplied) plus the chat session's current ride-state links and the optional
    # GPS pickup the frontend sent.
    tool_context = ToolContext(
        user_id=req.user_id,
        chat_session_id=chat_session.id,
        quote_session_id=context.quote_session_id,
        booking_id=context.booking_id,
        pickup_lat=req.location.lat if req.location else None,
        pickup_lng=req.location.lng if req.location else None,
    )

    async def event_stream() -> AsyncIterator[str]:
        """Produce the SSE frames for this turn and persist the reply."""
        # Lead with session_created on a brand-new conversation so the frontend
        # learns the chat_session_id and can open its GET stream.
        if is_new_session:
            yield format_sse(
                {
                    "type": "session_created",
                    "chat_session_id": str(chat_session.id),
                }
            )

        # Use a DB session scoped to THIS generator rather than the request-scoped
        # `db`. With a StreamingResponse, the request dependency is torn down when
        # the handler returns — before the body finishes streaming — which would
        # orphan its pooled connection. An explicit `async with` here guarantees
        # the connection is returned to the pool when streaming ends.
        async with database.session_factory() as stream_db:
            stream_repo = ChatRepository(stream_db)
            parts: list[str] = []
            async for chunk in responder.stream_reply(
                context, req.message, tool_context=tool_context, db=stream_db
            ):
                parts.append(chunk)
                yield format_sse({"type": "token", "content": chunk})

            # Persist the complete assistant message now that streaming is done.
            full_reply = "".join(parts)
            await stream_repo.add_message(
                chat_session.id, MessageRole.ASSISTANT, full_reply
            )

        yield format_sse({"type": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _stop_monitoring_if_abandoned(
    user_id: str, chat_session_id: uuid.UUID
) -> None:
    """Stop quote monitoring for a session once its SSE client has gone for good.

    Waits a short grace period and then, only if no client has re-subscribed for
    this chat session, cancels the linked quote session in the Quote Service.
    Best-effort: any failure is logged and swallowed.
    """
    await asyncio.sleep(MONITORING_STOP_GRACE_SECONDS)

    key = make_session_key(user_id, chat_session_id)
    if await event_bus.subscriber_count(key) > 0:
        # A client reconnected within the grace window — keep monitoring.
        return

    try:
        async with database.session_factory() as db:
            chat_session = await ChatRepository(db).get_chat_session(chat_session_id)
            quote_session_id = (
                chat_session.quote_session_id if chat_session else None
            )
    except Exception:  # noqa: BLE001 - cleanup must not raise
        logger.exception(
            "Failed to load chat session %s while stopping monitoring",
            chat_session_id,
        )
        return

    if quote_session_id is None:
        return

    result = await tools.cancel_quote_session(quote_session_id)
    if result.get("success"):
        logger.info(
            "Stopped monitoring quote session %s after client disconnect (chat %s)",
            quote_session_id,
            chat_session_id,
        )
    else:
        logger.warning(
            "Could not stop monitoring quote session %s: %s",
            quote_session_id,
            result.get("error"),
        )


@router.post("/api/sessions/{user_id}/{chat_session_id}/stop")
async def stop_session_monitoring(
    user_id: str,
    chat_session_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Stop quote monitoring for a chat session at the UI's explicit request.

    Resolves the chat session's linked quote session and cancels it in the Quote
    Service, taking it out of MONITORING so the worker stops refreshing it. It is
    lenient and idempotent: an unknown session, a chat with no active search, or
    an already-cancelled quote session all return a normal acknowledgement.
    """
    chat_session = await ChatRepository(db).get_chat_session(chat_session_id)
    quote_session_id = chat_session.quote_session_id if chat_session else None

    if quote_session_id is None:
        return {"status": "noop", "reason": "no active quote session"}

    result = await tools.cancel_quote_session(quote_session_id)
    if result.get("success"):
        logger.info(
            "Stopped monitoring quote session %s on UI request (chat %s, user %s)",
            quote_session_id,
            chat_session_id,
            user_id,
        )
        return {"status": "stopped", "quote_session_id": str(quote_session_id)}

    logger.info(
        "Stop-monitoring request for quote session %s was a no-op: %s",
        quote_session_id,
        result.get("error"),
    )
    return {"status": "noop", "reason": result.get("error")}


@router.get("/api/stream/{user_id}/{chat_session_id}")
async def stream_session_events(
    user_id: str, chat_session_id: uuid.UUID, request: Request
) -> StreamingResponse:
    """Open the SSE stream of server-pushed events for one chat session.

    Subscribes to the per-session event bus and relays server-initiated events
    (background ``quote_update`` / ``booking_update`` / ``ride_status`` updates
    and proactive ``ai_notification`` messages). The assistant's reply to a
    message is NOT carried here; it streams on the POST response. Each session has
    its own independent stream (Requirement 7.2). The subscription is always
    removed on disconnect so the event bus does not leak queues.
    """
    key: SessionKey = make_session_key(user_id, chat_session_id)

    async def event_stream() -> AsyncIterator[str]:
        """Relay published events to the client until it disconnects."""
        queue = await event_bus.subscribe(key)
        try:
            while True:
                # Stop promptly if the client has gone away.
                if await request.is_disconnected():
                    break
                try:
                    # Wake periodically even with no events so we can send a
                    # keepalive and re-check for disconnect.
                    event = await asyncio.wait_for(
                        queue.get(), timeout=SSE_KEEPALIVE_INTERVAL_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield _keepalive_comment()
                    continue
                yield format_sse(event)
        finally:
            # Always clean up the subscription, whatever ended the stream.
            await event_bus.unsubscribe(key, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
