"""Request and response models for the Booking Service API.

These Pydantic models define the shapes the AI Service (the only caller of the
Booking Service) sends and receives. ORM models in models.booking_models remain
the source of truth for persisted state; these schemas are the HTTP boundary
representation.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CreateBookingRequest(BaseModel):
    """Request body for POST /bookings.

    Carries the selected quote's context so the booking can later be re-verified
    and confirmed with the provider. Pickup/dropoff coordinates are optional but
    are required (non-null) to re-verify or confirm with the provider.
    """

    user_id: str
    provider: str
    ride_type: str
    selected_price: float

    chat_session_id: uuid.UUID | None = None
    quote_session_id: uuid.UUID | None = None
    quote_id: uuid.UUID | None = None

    pickup_address: str | None = None
    pickup_lat: float | None = None
    pickup_lng: float | None = None

    dropoff_address: str | None = None
    dropoff_lat: float | None = None
    dropoff_lng: float | None = None

    currency: str = "USD"
    pickup_eta_minutes: int | None = None


class ConfirmBookingRequest(BaseModel):
    """Request body for POST /bookings/{id}/confirm.

    Confirmation is never implicit: ``confirmed`` must be explicitly true or the
    request is rejected (Requirement 5.6). An optional idempotency_key may be
    supplied by the caller; when omitted, a stable key derived from the booking
    is used so retries never create a duplicate booking (Requirement 5.5).
    """

    confirmed: bool = False
    idempotency_key: str | None = None


class BookingOut(BaseModel):
    """A booking as returned to the caller."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: str
    chat_session_id: uuid.UUID | None = None
    quote_session_id: uuid.UUID | None = None
    quote_id: uuid.UUID | None = None

    provider: str
    ride_type: str

    pickup_address: str | None = None
    pickup_lat: float | None = None
    pickup_lng: float | None = None
    dropoff_address: str | None = None
    dropoff_lat: float | None = None
    dropoff_lng: float | None = None

    selected_price: float
    final_price: float | None = None
    currency: str
    pickup_eta_minutes: int | None = None

    status: str
    provider_booking_id: str | None = None

    created_at: datetime
    updated_at: datetime


class VerifyBookingResponse(BaseModel):
    """Response body for POST /bookings/{id}/verify.

    Surfaces the re-verified final price alongside the originally selected price
    so the caller can decide whether explicit re-confirmation is required
    (Requirement 5.3). ``price_changed`` is true when the final price differs
    from the selected price; ``price_difference`` is final minus selected.
    """

    booking: BookingOut
    selected_price: float
    final_price: float
    price_changed: bool
    price_difference: float
    currency: str


class ConfirmBookingResponse(BaseModel):
    """Response body for POST /bookings/{id}/confirm."""

    booking: BookingOut
    provider_booking_id: str
    final_price: float
    currency: str
    idempotent_replay: bool = False


class BookingEventOut(BaseModel):
    """A single booking event in the ride timeline."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    booking_id: uuid.UUID
    event_type: str
    sequence: int | None = None
    payload: dict | None = None
    created_at: datetime


class BookingEventsResponse(BaseModel):
    """Response body for GET /bookings/{id}/events."""

    booking_id: uuid.UUID
    events: list[BookingEventOut] = Field(default_factory=list)
