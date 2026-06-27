"""Data-access helpers for chat sessions, messages, and conversation context.

These functions are the persistence layer the AI Service request handling builds
on. They are intentionally thin wrappers over SQLAlchemy so the message handler
(task 6.2) and tool calling (task 6.3) can stay focused on orchestration:

  - create a chat session on the user's first message and return its id
    (Requirement 1.4) — see get_or_create_session / create_chat_session.
  - append user/assistant messages as the turn progresses — add_message.
  - load the last N messages plus ride state per request — load_context
    (Requirements 7.1, 7.2 rely on this being scoped per chat_session_id).

The number of messages kept as context defaults to 20 and is configurable via
the CHAT_CONTEXT_MESSAGE_LIMIT environment variable.
"""

from __future__ import annotations

import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ChatMessage, ChatSession, ChatSessionStatus, MessageRole
from schemas import ChatMessageOut, ConversationContext

# Default number of most-recent messages loaded as conversation context. Kept
# bounded so prompts stay within a reasonable size; override per-deployment via
# the CHAT_CONTEXT_MESSAGE_LIMIT env var.
DEFAULT_CONTEXT_MESSAGE_LIMIT = 20


def _context_message_limit() -> int:
    """Resolve the conversation context message limit from the environment.

    Returns:
        The configured limit, or DEFAULT_CONTEXT_MESSAGE_LIMIT when the env var
        is unset, non-numeric, or not positive.
    """
    raw = os.getenv("CHAT_CONTEXT_MESSAGE_LIMIT")
    if raw is None:
        return DEFAULT_CONTEXT_MESSAGE_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CONTEXT_MESSAGE_LIMIT
    return value if value > 0 else DEFAULT_CONTEXT_MESSAGE_LIMIT


async def create_chat_session(
    session: AsyncSession,
    user_id: str,
    *,
    quote_session_id: uuid.UUID | None = None,
    booking_id: uuid.UUID | None = None,
) -> ChatSession:
    """Create and persist a new chat session.

    The new session starts ACTIVE. Its generated id is the chat_session_id the
    caller returns to the frontend on the first message (Requirement 1.4).

    Args:
        session: Active database session.
        user_id: The owning user.
        quote_session_id: Optional linked quote session.
        booking_id: Optional linked booking.

    Returns:
        The newly created, refreshed ChatSession.
    """
    chat_session = ChatSession(
        user_id=user_id,
        status=ChatSessionStatus.ACTIVE.value,
        quote_session_id=quote_session_id,
        booking_id=booking_id,
    )
    session.add(chat_session)
    await session.commit()
    await session.refresh(chat_session)
    return chat_session


async def get_chat_session(
    session: AsyncSession, chat_session_id: uuid.UUID
) -> ChatSession | None:
    """Load a chat session by id.

    Args:
        session: Active database session.
        chat_session_id: The session id to load.

    Returns:
        The ChatSession, or None if no session with that id exists.
    """
    return await session.get(ChatSession, chat_session_id)


async def get_or_create_session(
    session: AsyncSession,
    user_id: str,
    chat_session_id: uuid.UUID | None = None,
) -> ChatSession:
    """Return the existing chat session, or create a new one when none is given.

    This implements "create a chat session on first message and return its id":
    when ``chat_session_id`` is None (the first message of a conversation) a new
    session is created and returned; otherwise the referenced session is loaded.

    Args:
        session: Active database session.
        user_id: The owning user (used when creating a new session).
        chat_session_id: Optional existing session id from the request.

    Returns:
        The existing or newly created ChatSession.

    Raises:
        LookupError: If a chat_session_id is provided but no such session exists.
    """
    if chat_session_id is None:
        return await create_chat_session(session, user_id)

    chat_session = await get_chat_session(session, chat_session_id)
    if chat_session is None:
        raise LookupError(f"Chat session {chat_session_id} not found")
    return chat_session


async def add_message(
    session: AsyncSession,
    chat_session_id: uuid.UUID,
    role: MessageRole,
    content: str,
) -> ChatMessage:
    """Persist a single chat message for a session.

    Args:
        session: Active database session.
        chat_session_id: The session the message belongs to.
        role: The message author (user or assistant).
        content: The message text.

    Returns:
        The newly created, refreshed ChatMessage.
    """
    message = ChatMessage(
        chat_session_id=chat_session_id,
        role=role.value if isinstance(role, MessageRole) else role,
        content=content,
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)
    return message


async def update_session_links(
    session: AsyncSession,
    chat_session: ChatSession,
    *,
    quote_session_id: uuid.UUID | None = None,
    booking_id: uuid.UUID | None = None,
) -> ChatSession:
    """Persist the chat session's links to its active quote session / booking.

    This is AI-service-owned *chat* state — it records which ride flow the
    conversation is currently driving so subsequent turns (especially the no-key
    stub path in responder.py) can resume mid-flow without re-deriving the ids.
    It is NOT a write to quote/booking/ride state, so it respects the tool-calling
    boundary: the Quote and Booking services remain the source of truth for the
    actual ride state (Requirements 9.1, 9.5). Only the provided ids are updated;
    passing ``None`` leaves the corresponding link unchanged.

    Args:
        session: Active database session.
        chat_session: The chat session to update (already loaded this turn).
        quote_session_id: New linked quote session id, or None to leave as-is.
        booking_id: New linked booking id, or None to leave as-is.

    Returns:
        The refreshed ChatSession.
    """
    if quote_session_id is not None:
        chat_session.quote_session_id = quote_session_id
    if booking_id is not None:
        chat_session.booking_id = booking_id

    session.add(chat_session)
    await session.commit()
    await session.refresh(chat_session)
    return chat_session


async def load_context(
    session: AsyncSession,
    chat_session_id: uuid.UUID,
    limit: int | None = None,
) -> ConversationContext:
    """Load the conversation context for a chat session.

    Returns the last N messages (oldest first, so they can be replayed directly
    as LLM context) together with the session's ride state (quote_session_id and
    booking_id). The fetch is scoped strictly by chat_session_id so concurrent
    sessions never bleed into each other (Requirements 7.1, 7.2).

    Args:
        session: Active database session.
        chat_session_id: The session to load context for.
        limit: Max number of most-recent messages to include. Defaults to the
            CHAT_CONTEXT_MESSAGE_LIMIT env var (or 20).

    Returns:
        A ConversationContext bundling session ride state and recent messages.

    Raises:
        LookupError: If no chat session with that id exists.
    """
    chat_session = await get_chat_session(session, chat_session_id)
    if chat_session is None:
        raise LookupError(f"Chat session {chat_session_id} not found")

    effective_limit = limit if limit is not None and limit > 0 else _context_message_limit()

    # Pull the most-recent messages by ordering newest-first with a limit, then
    # reverse to chronological order for replay. This keeps the query bounded
    # even for long conversations.
    result = await session.execute(
        select(ChatMessage)
        .where(ChatMessage.chat_session_id == chat_session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(effective_limit)
    )
    recent = list(result.scalars().all())
    recent.reverse()

    return ConversationContext(
        chat_session_id=chat_session.id,
        user_id=chat_session.user_id,
        status=chat_session.status,
        quote_session_id=chat_session.quote_session_id,
        booking_id=chat_session.booking_id,
        messages=[ChatMessageOut.model_validate(m) for m in recent],
    )
