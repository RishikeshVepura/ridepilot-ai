"""Data-access layer for chat sessions, messages, and conversation context.

The :class:`ChatRepository` is the persistence layer the AI Service request
handling builds on. It is intentionally thin over SQLAlchemy so the chat handler,
the responder, the LLM loop, and the event consumer can stay focused on
orchestration:

  - create a chat session on the user's first message and return its id
    (Requirement 1.4) — get_or_create_session / create_chat_session.
  - append user/assistant messages as the turn progresses — add_message.
  - load the context window per request — load_context. The window is the last
    N stored messages, in chronological order, plus the chat session's linked
    ride-state ids (Requirements 7.1, 7.2 rely on this being scoped per
    chat_session_id).

The number of messages kept as context defaults to 20 and is configurable via
the CHAT_CONTEXT_MESSAGE_LIMIT environment variable.
"""

from __future__ import annotations

import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.chat_models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    MessageRole,
)
from schemas.chat_schemas import ChatMessageOut, ConversationContext

# Default number of most-recent stored messages loaded into the LLM context
# window. The window also includes the chat session's linked ride-state ids; this
# limit applies only to replayed chat messages. A small window is deliberate:
# every replayed message is input to every model/tool round.
DEFAULT_CONTEXT_MESSAGE_LIMIT = 6
# Never let an environment setting turn a long chat into unbounded model input.
MAX_CONTEXT_MESSAGE_LIMIT = 6
CONTEXT_MESSAGE_LIMIT_ENV = "CHAT_CONTEXT_MESSAGE_LIMIT"


def _context_message_limit() -> int:
    """Resolve the conversation context message limit from the environment.

    Returns:
        The configured limit, or DEFAULT_CONTEXT_MESSAGE_LIMIT when the env var
        is unset, non-numeric, or not positive.
    """
    raw = os.getenv(CONTEXT_MESSAGE_LIMIT_ENV)
    if raw is None:
        return DEFAULT_CONTEXT_MESSAGE_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CONTEXT_MESSAGE_LIMIT
    if value <= 0:
        return DEFAULT_CONTEXT_MESSAGE_LIMIT
    return min(value, MAX_CONTEXT_MESSAGE_LIMIT)


class ChatRepository:
    """Database access for chat sessions, messages, and context over one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to an active database session.

        Args:
            session: The AsyncSession this repository reads and writes through.
        """
        self.session = session

    async def create_chat_session(
        self,
        user_id: str,
        *,
        quote_session_id: uuid.UUID | None = None,
        booking_id: uuid.UUID | None = None,
    ) -> ChatSession:
        """Create and persist a new chat session.

        The new session starts ACTIVE. Its generated id is the chat_session_id the
        caller returns to the frontend on the first message (Requirement 1.4).
        """
        chat_session = ChatSession(
            user_id=user_id,
            status=ChatSessionStatus.ACTIVE.value,
            quote_session_id=quote_session_id,
            booking_id=booking_id,
        )
        self.session.add(chat_session)
        await self.session.commit()
        await self.session.refresh(chat_session)
        return chat_session

    async def get_chat_session(
        self, chat_session_id: uuid.UUID
    ) -> ChatSession | None:
        """Load a chat session by id, or None when no such session exists."""
        return await self.session.get(ChatSession, chat_session_id)

    async def get_or_create_session(
        self, user_id: str, chat_session_id: uuid.UUID | None = None
    ) -> ChatSession:
        """Return the existing chat session, or create a new one when none is given.

        When ``chat_session_id`` is None (the first message of a conversation) a
        new session is created and returned; otherwise the referenced session is
        loaded.

        Raises:
            LookupError: If a chat_session_id is provided but no such session
                exists.
        """
        if chat_session_id is None:
            return await self.create_chat_session(user_id)

        chat_session = await self.get_chat_session(chat_session_id)
        if chat_session is None:
            raise LookupError(f"Chat session {chat_session_id} not found")
        return chat_session

    async def add_message(
        self, chat_session_id: uuid.UUID, role: MessageRole, content: str
    ) -> ChatMessage:
        """Persist a single chat message for a session."""
        message = ChatMessage(
            chat_session_id=chat_session_id,
            role=role.value if isinstance(role, MessageRole) else role,
            content=content,
        )
        self.session.add(message)
        await self.session.commit()
        await self.session.refresh(message)
        return message

    async def update_session_links(
        self,
        chat_session: ChatSession,
        *,
        quote_session_id: uuid.UUID | None = None,
        booking_id: uuid.UUID | None = None,
    ) -> ChatSession:
        """Persist the chat session's links to its active quote session / booking.

        This is AI-service-owned *chat* state — it records which ride flow the
        conversation is currently driving so subsequent turns can resume mid-flow
        without re-deriving the ids. It is NOT a write to quote/booking/ride
        state, so it respects the tool-calling boundary: the Quote and Booking
        services remain the source of truth (Requirements 9.1, 9.5). Only the
        provided ids are updated; passing None leaves the link unchanged.
        """
        if quote_session_id is not None:
            chat_session.quote_session_id = quote_session_id
        if booking_id is not None:
            chat_session.booking_id = booking_id

        self.session.add(chat_session)
        await self.session.commit()
        await self.session.refresh(chat_session)
        return chat_session

    async def load_context(
        self, chat_session_id: uuid.UUID, limit: int | None = None
    ) -> ConversationContext:
        """Load the context window for a chat session.

        The context window is the bounded, per-session state replayed into a chat
        turn: the last N stored messages (oldest first, so they can be sent
        directly to the LLM) plus the chat session's linked ride-state ids. The
        fetch is scoped strictly by chat_session_id so concurrent sessions never
        bleed into each other (Requirements 7.1, 7.2).

        Raises:
            LookupError: If no chat session with that id exists.
        """
        chat_session = await self.get_chat_session(chat_session_id)
        if chat_session is None:
            raise LookupError(f"Chat session {chat_session_id} not found")

        effective_limit = (
            limit if limit is not None and limit > 0 else _context_message_limit()
        )

        # Pull the most-recent messages newest-first with a limit, then reverse to
        # chronological order for replay. Keeps the query bounded for long chats.
        result = await self.session.execute(
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
