"""Public chat + streaming endpoints for the AI Service.

These are the only endpoints the frontend talks to (design rule 1). Together
they implement the chat session lifecycle from design section 7:

    POST /api/chat/message
        The user sends a message. The handler resolves (or creates, on the first
        message) the chat session, persists the user message, loads conversation
        context, and streams the assistant's reply back to the caller as SSE —
        token by token, ending with a ``done`` event. The very first response of
        a new conversation leads with a ``session_created`` event carrying the
        new chat_session_id so the frontend learns which stream to open.

    GET /api/stream/{user_id}/{chat_session_id}
        A long-lived SSE stream of all server-pushed events for one chat session
        (Requirement 7.2 — one stream per session, scoped by chat_session_id).
        Subscribes to the per-session event bus and relays events as they are
        published, with periodic keepalive comments so idle connections stay up.

Both the POST reply tokens and any background updates are published to the same
:data:`event_bus` keyed by ``(user_id, chat_session_id)`` so the dedicated GET
stream stays in sync with what the POST caller receives, and so the event
consumer added in task 6.4 can push to the same stream.

Requirements:
  1.1, 1.2, 1.3 — stream the assistant's response to a user message back to the
    frontend as it is generated.
  2.4 — the streamed-token mechanism is what carries the AI's quote summary once
    the LLM/tool layer (task 6.3) can produce it; a stub reply exercises it now.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

import repository
from db import get_session
from event_bus import SessionKey, event_bus, make_session_key
from models import MessageRole
from responder import stream_assistant_reply
from schemas import ChatMessageRequest
from tools import ToolContext

router = APIRouter(tags=["chat"])

# How often (seconds) to emit an SSE keepalive comment on an idle stream. Comment
# lines (": ...") are ignored by EventSource clients but keep the TCP connection
# and any intermediary proxies from timing the stream out.
SSE_KEEPALIVE_INTERVAL_SECONDS = 15.0

# Standard SSE response headers. Disable caching and proxy buffering so events
# are delivered immediately rather than batched (X-Accel-Buffering is honored by
# nginx), and keep the connection alive for the life of the stream.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def format_sse(event: dict) -> str:
    """Serialize an event dict as a single SSE ``data:`` frame.

    Produces ``data: <json>\\n\\n`` — the JSON payload on one data line followed
    by the blank line that terminates the event, as required by the SSE wire
    format.

    Args:
        event: A JSON-serializable event payload (e.g. ``{"type": "token",
            "content": "Lyft "}``).

    Returns:
        The formatted SSE frame, ready to be yielded by a streaming response.
    """
    return f"data: {json.dumps(event)}\n\n"


def _keepalive_comment() -> str:
    """Return an SSE comment frame used to keep an idle connection open.

    Comment frames (lines beginning with ``:``) are part of the SSE spec and are
    silently ignored by clients, making them ideal heartbeats.
    """
    return ": keepalive\n\n"


@router.post("/api/chat/message")
async def post_chat_message(
    req: ChatMessageRequest, db: AsyncSession = Depends(get_session)
) -> StreamingResponse:
    """Handle a user message and stream the assistant's reply back as SSE.

    Lifecycle (design section 7):
      1. Resolve the chat session, or create one when ``chat_session_id`` is null
         (the first message of a conversation).
      2. Persist the incoming user message.
      3. Load the conversation context (recent messages + ride state).
      4. Return a ``text/event-stream`` response that, as it is consumed:
           - emits ``session_created`` first when a new session was just created,
           - streams the assistant reply as ``token`` events,
           - then emits a ``done`` event.
         The full assistant message is persisted once streaming completes.

    Every event streamed to the caller is also published to the per-session event
    bus so the separate ``GET /api/stream`` connection stays in sync. The session
    is created exactly once here (not again by the GET stream).

    Args:
        req: The chat message request (user, optional chat_session_id, message,
            optional location).
        db: Active database session (held open for the duration of the stream).

    Returns:
        A StreamingResponse of SSE frames for this turn.

    Raises:
        HTTPException: 404 if a chat_session_id is supplied but no such session
            exists.
    """
    # Steps 1-3 run before we start streaming so input errors (e.g. an unknown
    # session id) surface as a normal HTTP error response rather than mid-stream.
    is_new_session = req.chat_session_id is None
    try:
        chat_session = await repository.get_or_create_session(
            db, req.user_id, req.chat_session_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await repository.add_message(
        db, chat_session.id, MessageRole.USER, req.message
    )

    context = await repository.load_context(db, chat_session.id)

    # Build the trusted tool context for this turn: identity (never model-
    # supplied) plus the chat session's current ride-state links and the optional
    # GPS pickup the frontend sent. This is what the LLM/stub seam uses to drive
    # the backend tools as the right user (Requirement 9.4) and resume mid-flow.
    tool_context = ToolContext(
        user_id=req.user_id,
        chat_session_id=chat_session.id,
        quote_session_id=context.quote_session_id,
        booking_id=context.booking_id,
        pickup_lat=req.location.lat if req.location else None,
        pickup_lng=req.location.lng if req.location else None,
    )

    key = make_session_key(req.user_id, chat_session.id)

    async def event_stream() -> AsyncIterator[str]:
        """Produce the SSE frames for this turn and persist the reply."""
        # Lead with session_created on a brand-new conversation so the frontend
        # learns the chat_session_id and can open its GET stream.
        if is_new_session:
            created_event = {
                "type": "session_created",
                "chat_session_id": str(chat_session.id),
            }
            await event_bus.publish(key, created_event)
            yield format_sse(created_event)

        # Stream the assistant reply token by token, fanning each token out to
        # the session's GET stream as well, and accumulate the full text.
        parts: list[str] = []
        async for chunk in stream_assistant_reply(
            context, req.message, tool_context=tool_context, db=db
        ):
            parts.append(chunk)
            token_event = {"type": "token", "content": chunk}
            await event_bus.publish(key, token_event)
            yield format_sse(token_event)

        # Persist the complete assistant message now that streaming is done.
        full_reply = "".join(parts)
        await repository.add_message(
            db, chat_session.id, MessageRole.ASSISTANT, full_reply
        )

        done_event = {"type": "done"}
        await event_bus.publish(key, done_event)
        yield format_sse(done_event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/api/stream/{user_id}/{chat_session_id}")
async def stream_session_events(
    user_id: str, chat_session_id: uuid.UUID, request: Request
) -> StreamingResponse:
    """Open the SSE stream of server-pushed events for one chat session.

    The frontend opens this once it knows the chat_session_id (from the first
    POST response's ``session_created`` event). The handler subscribes to the
    per-session event bus and relays every published event — assistant tokens
    from the active turn plus background ``quote_update`` / ``ai_notification`` /
    ``booking_update`` / ``ride_status`` events (pushed by task 6.4) — as SSE
    frames. Each session has its own independent stream (Requirement 7.2).

    The connection stays open until the client disconnects; a keepalive comment
    is sent during idle periods. On disconnect the subscription is always removed
    so the event bus does not leak queues.

    Args:
        user_id: The owning user (path segment).
        chat_session_id: The chat session whose events to stream (path segment).
        request: The incoming request, used to detect client disconnects.

    Returns:
        A StreamingResponse of SSE frames for the session.
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
            # Always clean up the subscription, whether the client disconnected,
            # the task was cancelled, or an error propagated.
            await event_bus.unsubscribe(key, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
