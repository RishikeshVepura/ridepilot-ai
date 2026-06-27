"""SQLAlchemy ORM models for the Booking Service.

Maps the bookings and booking_events tables described in the design document
(section 10). The Booking Service is the source of truth for all booking and
ride lifecycle state (Requirement 9.5).
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


class BookingStatus(str, enum.Enum):
    """Lifecycle states for a booking.

    Covers the full flow from creation through final-price verification,
    explicit confirmation, the post-confirmation ride milestones, and the
    terminal cancelled/failed/expired states described in the design document.
    """

    CREATED = "CREATED"
    PREPARING = "PREPARING"
    FINAL_PRICE_VERIFIED = "FINAL_PRICE_VERIFIED"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    CONFIRMING = "CONFIRMING"
    CONFIRMED = "CONFIRMED"
    DRIVER_ASSIGNMENT_PENDING = "DRIVER_ASSIGNMENT_PENDING"
    DRIVER_ASSIGNED = "DRIVER_ASSIGNED"
    DRIVER_ARRIVING = "DRIVER_ARRIVING"
    RIDE_STARTED = "RIDE_STARTED"
    RIDE_COMPLETED = "RIDE_COMPLETED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class BookingEventType(str, enum.Enum):
    """Types of events recorded against a booking.

    Captures the booking history end to end: creation, final price
    verification, confirmation, the ride status milestones, and the
    cancelled/failed terminal events.
    """

    BOOKING_CREATED = "BOOKING_CREATED"
    FINAL_PRICE_VERIFIED = "FINAL_PRICE_VERIFIED"
    BOOKING_CONFIRMED = "BOOKING_CONFIRMED"
    DRIVER_ASSIGNED = "DRIVER_ASSIGNED"
    DRIVER_ARRIVING = "DRIVER_ARRIVING"
    RIDE_STARTED = "RIDE_STARTED"
    RIDE_COMPLETED = "RIDE_COMPLETED"
    BOOKING_CANCELLED = "BOOKING_CANCELLED"
    BOOKING_FAILED = "BOOKING_FAILED"


class Booking(Base):
    """A booking session that owns its lifecycle events.

    Created when a user initiates booking for a selected quote, re-verifies the
    final price with the provider, and is only confirmed with explicit user
    approval using an idempotency key.
    """

    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    chat_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    quote_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    provider: Mapped[str] = mapped_column(String, nullable=False)
    ride_type: Mapped[str] = mapped_column(String, nullable=False)

    pickup_address: Mapped[str | None] = mapped_column(String, nullable=True)
    pickup_lat: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    pickup_lng: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)

    dropoff_address: Mapped[str | None] = mapped_column(String, nullable=True)
    dropoff_lat: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    dropoff_lng: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)

    selected_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    final_price: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    pickup_eta_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[BookingStatus] = mapped_column(
        String, nullable=False, default=BookingStatus.CREATED.value
    )

    # Set after the provider confirms the booking.
    provider_booking_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Used to make confirmation idempotent and prevent duplicate bookings.
    idempotency_key: Mapped[str | None] = mapped_column(
        String, nullable=True, unique=True
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

    events: Mapped[list["BookingEvent"]] = relationship(
        back_populates="booking", cascade="all, delete-orphan"
    )


class BookingEvent(Base):
    """An event recorded against a booking (creation, verification, milestones)."""

    __tablename__ = "booking_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type: Mapped[BookingEventType] = mapped_column(String, nullable=False)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    booking: Mapped["Booking"] = relationship(back_populates="events")
