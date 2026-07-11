"""SQLAlchemy ORM models for the Quote Service.

Maps the quote_sessions, quotes, and quote_events tables described in the
design document. The Quote Service is the source of truth for all quote state.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base


class QuoteSessionStatus(str, enum.Enum):
    """Lifecycle states for a quote session."""

    CREATED = "CREATED"
    FETCHING_QUOTES = "FETCHING_QUOTES"
    MONITORING = "MONITORING"
    QUOTE_SELECTED = "QUOTE_SELECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class QuoteEventType(str, enum.Enum):
    """Types of events recorded against a quote session.

    The first group (snapshots, deltas, ranking changes, availability, expiry)
    comes from the design document and is produced primarily by quote fetching
    and the monitoring worker. The lifecycle group records meaningful session
    actions (created, a quote selected, session cancelled) so the session's
    history is captured end to end.
    """

    QUOTE_SNAPSHOT = "QUOTE_SNAPSHOT"
    QUOTE_DELTA = "QUOTE_DELTA"
    NEW_CHEAPEST = "NEW_CHEAPEST"
    NEW_FASTEST = "NEW_FASTEST"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"

    # Session lifecycle events.
    SESSION_CREATED = "SESSION_CREATED"
    QUOTE_SELECTED = "QUOTE_SELECTED"
    SESSION_CANCELLED = "SESSION_CANCELLED"


class QuoteSession(Base):
    """A ride search session that owns a set of quotes and their events."""

    __tablename__ = "quote_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    chat_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    pickup_address: Mapped[str | None] = mapped_column(String, nullable=True)
    pickup_lat: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    pickup_lng: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)

    dropoff_address: Mapped[str | None] = mapped_column(String, nullable=True)
    dropoff_lat: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    dropoff_lng: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)

    status: Mapped[QuoteSessionStatus] = mapped_column(
        String, nullable=False, default=QuoteSessionStatus.CREATED.value
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    quotes: Mapped[list["Quote"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    events: Mapped[list["QuoteEvent"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Quote(Base):
    """A single normalized quote from a provider for one ride type."""

    __tablename__ = "quotes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quote_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quote_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    provider: Mapped[str] = mapped_column(String, nullable=False)
    ride_type: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    pickup_eta_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trip_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    session: Mapped["QuoteSession"] = relationship(back_populates="quotes")


class QuoteEvent(Base):
    """An event recorded against a quote session (snapshots, deltas, etc.)."""

    __tablename__ = "quote_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quote_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quote_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quotes.id", ondelete="CASCADE"),
        nullable=True,
    )

    event_type: Mapped[QuoteEventType] = mapped_column(String, nullable=False)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped["QuoteSession"] = relationship(back_populates="events")
