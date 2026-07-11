"""Business logic for the Booking Service.

The :class:`BookingService` implements the booking lifecycle use cases the AI
Service drives: create, verify, confirm (idempotent, explicit-approval-only),
cancel, and read the ride timeline. It orchestrates the repository (DB) and the
provider client (upstream calls) and owns the transaction for each use case.

It is deliberately HTTP-agnostic: on failure it raises the domain exceptions in
core.exceptions, which the API layer maps to HTTP status codes.

Requirements:
  5.1 — create a booking session when a user initiates booking.
  5.2 — re-verify the final price with the provider before confirmation.
  5.3 — surface a changed final price so re-confirmation can be required.
  5.4 — confirm the booking with the provider on explicit approval.
  5.5 — use an idempotency key to prevent duplicate bookings.
  5.6 — never confirm without explicit user approval.
  6.4 — cancel a confirmed booking with the provider and mark it CANCELLED.
  7.3 — a user may not hold two concurrent active bookings with the same provider.
"""

from __future__ import annotations

import uuid

from core.exceptions import (
    BookingNotFoundError,
    ConfirmationNotExplicitError,
    ConflictingBookingError,
    InvalidBookingStatusError,
    MissingCoordinatesError,
    ProviderUnavailableError,
)
from models.booking_models import (
    Booking,
    BookingEvent,
    BookingEventType,
    BookingStatus,
)
from provider.provider import ProviderClient, ProviderError
from repositories.booking_repository import BookingRepository
from schemas.booking_schemas import (
    BookingEventOut,
    BookingEventsResponse,
    BookingOut,
    ConfirmBookingRequest,
    ConfirmBookingResponse,
    CreateBookingRequest,
    VerifyBookingResponse,
)


class BookingService:
    """Coordinates the repository and provider client to run booking use cases."""

    def __init__(
        self, repository: BookingRepository, provider_client: ProviderClient
    ) -> None:
        """Wire the service to its data-access and provider dependencies.

        Args:
            repository: Data-access layer for bookings and events.
            provider_client: Client for upstream provider calls.
        """
        self.repository = repository
        self.provider = provider_client

    async def create_booking(self, req: CreateBookingRequest) -> Booking:
        """Create a new booking session and record a BOOKING_CREATED event.

        The booking starts in CREATED status so it can later be re-verified and
        confirmed with the provider. (Requirement 5.1)

        Args:
            req: The booking details (user, provider, ride type, selected price,
                pickup/dropoff context).

        Returns:
            The newly created Booking.
        """
        booking = Booking(
            user_id=req.user_id,
            chat_session_id=req.chat_session_id,
            quote_session_id=req.quote_session_id,
            quote_id=req.quote_id,
            provider=req.provider,
            ride_type=req.ride_type,
            pickup_address=req.pickup_address,
            pickup_lat=req.pickup_lat,
            pickup_lng=req.pickup_lng,
            dropoff_address=req.dropoff_address,
            dropoff_lat=req.dropoff_lat,
            dropoff_lng=req.dropoff_lng,
            selected_price=req.selected_price,
            currency=req.currency,
            pickup_eta_minutes=req.pickup_eta_minutes,
            status=BookingStatus.CREATED.value,
        )
        self.repository.add(booking)
        await self.repository.flush()

        self.repository.add(
            BookingEvent(
                booking_id=booking.id,
                event_type=BookingEventType.BOOKING_CREATED.value,
                sequence=1,
                payload={
                    "user_id": req.user_id,
                    "provider": req.provider,
                    "ride_type": req.ride_type,
                    "selected_price": req.selected_price,
                    "currency": req.currency,
                },
            )
        )

        await self.repository.commit()
        await self.repository.refresh(booking)
        return booking

    async def verify_booking(self, booking_id: uuid.UUID) -> VerifyBookingResponse:
        """Re-verify the final price with the provider before confirmation.

        Re-quotes the provider for the selected ride type, stores the verified
        final price, and moves the booking to FINAL_PRICE_VERIFIED. The response
        surfaces the selected vs. final price and whether they differ so the
        caller can require explicit re-confirmation. (Requirements 5.2, 5.3)

        Args:
            booking_id: The booking to verify.

        Returns:
            VerifyBookingResponse with the booking, both prices, and the delta.

        Raises:
            BookingNotFoundError: If the booking does not exist.
            MissingCoordinatesError: If pickup/dropoff coordinates are absent.
            ProviderUnavailableError: If the provider call fails.
        """
        booking = await self._get_or_raise(booking_id)
        pickup_lat, pickup_lng, dropoff_lat, dropoff_lng = self._require_coords(booking)

        try:
            verified = await self.provider.verify_price(
                provider=booking.provider,
                ride_type=booking.ride_type,
                pickup_lat=pickup_lat,
                pickup_lng=pickup_lng,
                dropoff_lat=dropoff_lat,
                dropoff_lng=dropoff_lng,
            )
        except ProviderError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

        selected_price = float(booking.selected_price)
        final_price = float(verified.price)
        price_difference = round(final_price - selected_price, 2)
        price_changed = price_difference != 0

        booking.final_price = final_price
        booking.currency = verified.currency
        if verified.pickup_eta_minutes is not None:
            booking.pickup_eta_minutes = verified.pickup_eta_minutes
        booking.status = BookingStatus.FINAL_PRICE_VERIFIED.value
        await self.repository.flush()

        sequence = await self.repository.next_event_sequence(booking_id)
        self.repository.add(
            BookingEvent(
                booking_id=booking.id,
                event_type=BookingEventType.FINAL_PRICE_VERIFIED.value,
                sequence=sequence,
                payload={
                    "selected_price": selected_price,
                    "final_price": final_price,
                    "price_changed": price_changed,
                    "price_difference": price_difference,
                    "currency": verified.currency,
                },
            )
        )

        await self.repository.commit()
        await self.repository.refresh(booking)

        return VerifyBookingResponse(
            booking=BookingOut.model_validate(booking),
            selected_price=selected_price,
            final_price=final_price,
            price_changed=price_changed,
            price_difference=price_difference,
            currency=verified.currency,
        )

    async def confirm_booking(
        self, booking_id: uuid.UUID, req: ConfirmBookingRequest
    ) -> ConfirmBookingResponse:
        """Confirm a booking with the provider, idempotently and only on approval.

        Confirmation is never implicit: ``confirmed`` must be explicitly true
        (Requirement 5.6). A stable idempotency key is derived from the booking id
        when the caller does not supply one and is persisted so retries never
        create a duplicate (Requirement 5.5). If the booking is already CONFIRMED
        with a provider_booking_id, the existing booking is returned with
        ``idempotent_replay=true``. Otherwise the provider is asked to confirm and
        the booking moves to CONFIRMED. (Requirements 5.4, 5.5, 5.6)

        Args:
            booking_id: The booking to confirm.
            req: The confirmation request carrying explicit approval and an
                optional idempotency key.

        Returns:
            ConfirmBookingResponse with the booking, provider_booking_id, final
            price, and whether this was an idempotent replay.

        Raises:
            BookingNotFoundError: If the booking does not exist.
            ConfirmationNotExplicitError: If confirmation is not explicit.
            InvalidBookingStatusError: If the booking is cancelled/failed/expired.
            MissingCoordinatesError: If pickup/dropoff coordinates are absent.
            ConflictingBookingError: If the user already has an active booking for
                this provider.
            ProviderUnavailableError: If the provider call fails.
        """
        booking = await self._get_or_raise(booking_id)

        # Explicit confirmation is mandatory — never confirm implicitly.
        if not req.confirmed:
            raise ConfirmationNotExplicitError(
                "Explicit confirmation is required (confirmed must be true)"
            )

        # Cancelled/failed/expired bookings cannot be confirmed.
        if booking.status in (
            BookingStatus.CANCELLED.value,
            BookingStatus.FAILED.value,
            BookingStatus.EXPIRED.value,
        ):
            raise InvalidBookingStatusError(
                f"Cannot confirm a booking in status {booking.status}"
            )

        # Stable idempotency key: caller-supplied, else derived from the booking
        # id so retries of the same booking always dedupe to the same provider
        # booking.
        idempotency_key = req.idempotency_key or f"booking-{booking.id}"
        if booking.idempotency_key is None:
            booking.idempotency_key = idempotency_key
            await self.repository.flush()
        else:
            # Once persisted, the booking's own key is authoritative for retries.
            idempotency_key = booking.idempotency_key

        # Idempotent replay: already confirmed with a provider booking — return it
        # without creating a second provider booking.
        if (
            booking.status == BookingStatus.CONFIRMED.value
            and booking.provider_booking_id is not None
        ):
            return ConfirmBookingResponse(
                booking=BookingOut.model_validate(booking),
                provider_booking_id=booking.provider_booking_id,
                final_price=float(booking.final_price)
                if booking.final_price is not None
                else float(booking.selected_price),
                currency=booking.currency,
                idempotent_replay=True,
            )

        pickup_lat, pickup_lng, dropoff_lat, dropoff_lng = self._require_coords(booking)

        # A user may not hold two concurrent active bookings with the same
        # provider. Checked just before confirming so verified-but-unconfirmed
        # bookings don't block each other. (Requirement 7.3)
        conflicting_id = await self.repository.find_conflicting_active_booking(
            booking.user_id, booking.provider, booking.id
        )
        if conflicting_id is not None:
            raise ConflictingBookingError(
                f"User already has an active booking with provider "
                f"'{booking.provider}' (booking {conflicting_id})"
            )

        try:
            provider_booking = await self.provider.confirm_booking(
                provider=booking.provider,
                ride_type=booking.ride_type,
                idempotency_key=idempotency_key,
                pickup_lat=pickup_lat,
                pickup_lng=pickup_lng,
                dropoff_lat=dropoff_lat,
                dropoff_lng=dropoff_lng,
            )
        except ProviderError as exc:
            # Leave the booking in its current (pre-confirmation) state rather
            # than marking it CONFIRMED. The persisted idempotency key makes a
            # retry safe.
            raise ProviderUnavailableError(str(exc)) from exc

        booking.provider_booking_id = provider_booking.provider_booking_id
        booking.final_price = float(provider_booking.final_price)
        booking.currency = provider_booking.currency
        if provider_booking.pickup_eta_minutes is not None:
            booking.pickup_eta_minutes = provider_booking.pickup_eta_minutes
        booking.status = BookingStatus.CONFIRMED.value
        await self.repository.flush()

        sequence = await self.repository.next_event_sequence(booking_id)
        self.repository.add(
            BookingEvent(
                booking_id=booking.id,
                event_type=BookingEventType.BOOKING_CONFIRMED.value,
                sequence=sequence,
                payload={
                    "provider_booking_id": provider_booking.provider_booking_id,
                    "final_price": float(provider_booking.final_price),
                    "currency": provider_booking.currency,
                    "idempotency_key": idempotency_key,
                },
            )
        )

        await self.repository.commit()
        await self.repository.refresh(booking)

        return ConfirmBookingResponse(
            booking=BookingOut.model_validate(booking),
            provider_booking_id=provider_booking.provider_booking_id,
            final_price=float(provider_booking.final_price),
            currency=provider_booking.currency,
            idempotent_replay=False,
        )

    async def cancel_booking(self, booking_id: uuid.UUID) -> Booking:
        """Cancel a booking, asking the provider to cancel a confirmed one first.

        Marks the booking CANCELLED and records a BOOKING_CANCELLED event.
        Cancelling an already-cancelled booking is a no-op that returns the
        current state. (Requirement 6.4)

        Args:
            booking_id: The booking to cancel.

        Returns:
            The cancelled Booking.

        Raises:
            BookingNotFoundError: If the booking does not exist.
            ProviderUnavailableError: If the provider cancellation fails.
        """
        booking = await self._get_or_raise(booking_id)

        # Cancelling an already-cancelled booking is a no-op.
        if booking.status == BookingStatus.CANCELLED.value:
            return booking

        previous_status = booking.status

        # Only call the provider if there is a confirmed provider booking.
        if (
            booking.status == BookingStatus.CONFIRMED.value
            and booking.provider_booking_id is not None
        ):
            try:
                await self.provider.cancel_booking(
                    provider=booking.provider,
                    provider_booking_id=booking.provider_booking_id,
                )
            except ProviderError as exc:
                raise ProviderUnavailableError(str(exc)) from exc

        booking.status = BookingStatus.CANCELLED.value
        await self.repository.flush()

        sequence = await self.repository.next_event_sequence(booking_id)
        self.repository.add(
            BookingEvent(
                booking_id=booking.id,
                event_type=BookingEventType.BOOKING_CANCELLED.value,
                sequence=sequence,
                payload={
                    "previous_status": previous_status,
                    "provider_booking_id": booking.provider_booking_id,
                },
            )
        )

        await self.repository.commit()
        await self.repository.refresh(booking)
        return booking

    async def get_booking_events(
        self, booking_id: uuid.UUID
    ) -> BookingEventsResponse:
        """Return the ride timeline (events) for a booking.

        Args:
            booking_id: The booking whose events to return.

        Returns:
            BookingEventsResponse with the ordered list of events.

        Raises:
            BookingNotFoundError: If the booking does not exist.
        """
        await self._get_or_raise(booking_id)
        events = await self.repository.list_events(booking_id)
        return BookingEventsResponse(
            booking_id=booking_id,
            events=[BookingEventOut.model_validate(e) for e in events],
        )

    async def _get_or_raise(self, booking_id: uuid.UUID) -> Booking:
        """Load a booking or raise BookingNotFoundError."""
        booking = await self.repository.get_by_id(booking_id)
        if booking is None:
            raise BookingNotFoundError(booking_id)
        return booking

    @staticmethod
    def _require_coords(booking: Booking) -> tuple[float, float, float, float]:
        """Return pickup/dropoff coordinates or raise MissingCoordinatesError.

        Pickup/dropoff coordinates are optional on a booking but are required to
        talk to the provider for verification or confirmation.
        """
        if (
            booking.pickup_lat is None
            or booking.pickup_lng is None
            or booking.dropoff_lat is None
            or booking.dropoff_lng is None
        ):
            raise MissingCoordinatesError(
                "Pickup and dropoff coordinates are required for this action"
            )
        return (
            float(booking.pickup_lat),
            float(booking.pickup_lng),
            float(booking.dropoff_lat),
            float(booking.dropoff_lng),
        )
