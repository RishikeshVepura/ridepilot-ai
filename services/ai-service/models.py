"""SQLAlchemy ORM models for the AI Service.

Maps the chat_sessions and chat_messages tables described in the design document
(section 10, AI Service Tables). The AI Service is the source of truth for chat
conversation state: each chat session may be linked to a quote session and/or a
booking, and owns the ordered messages exchanged with the user.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


class ChatSessionStatus(str, enum.Enum):
    """Lifecycle states for a chat session."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class MessageRole(str, enum.Enum):
    """Author of a chat message.

    Values match the roles expected by the LLM chat API so stored messages can
    be replayed directly as conversation context.
    """

    USER = "user"
    ASSISTANT = "assistant"


class ChatSession(Base):
    """A chat conversation that owns its ordered messages.

    Created on the user's first message (Requirement 1.4) and may be linked to a
    quote session and/or a booking as the conversation progresses. A user can
    have many active sessions at once, each with its own SSE stream scoped by id
    (Requirements 7.1, 7.2).
    """

    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[ChatSessionStatus] = mapped_column(
        String, nullable=False, default=ChatSessionStatus.ACTIVE.value
    )

    # Ride state linked to this conversation. Both are nullable: a session has no
    # quote session until the user requests rides, and no booking until one is
    # created. Together they form the "ride state" half of conversation context.
    quote_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )


class ChatMessage(Base):
    """A single message exchanged with the user, authored by user or assistant."""

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    chat_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    role: Mapped[MessageRole] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped["ChatSession"] = relationship(back_populates="messages")
