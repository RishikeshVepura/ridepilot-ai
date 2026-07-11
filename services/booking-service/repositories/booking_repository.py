"""Data-access layer for the Booking Service.

The :class:`BookingRepository` is the ONLY place that issues database queries for
bookings and booking events. It contains no business rules — just reads, writes,
and transaction control over a single :class:`AsyncSession`. The service layer
composes these methods to implement use cases.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.booking_models import (
    ACTIVE_BOOKING_STATUSES,
    Booking,
    BookingEvent,
)


class BookingRepository:
    """Database access for bookings and their events over one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to an active session.

        Args:
            session: The AsyncSession this repository reads and writes through.
        """
        self.session = session

    async def get_by_id(self, booking_id: uuid.UUID) -> Booking | None:
        """Return the booking with this id, or None when it does not exist."""
        return await self.session.get(Booking, booking_id)

    def add(self, instance: object) -> None:
        """Stage a new ORM instance (Booking or BookingEvent) for insertion."""
        self.session.add(instance)

    async def next_event_sequence(self, booking_id: uuid.UUID) -> int:
        """Return the next per-booking event sequence number (max existing + 1).

        Events are numbered per booking starting at 1 so their order is stable
        and independent of timestamp resolution.
        """
        result = await self.session.execute(
            select(func.coalesce(func.max(BookingEvent.sequence), 0)).where(
                BookingEvent.booking_id == booking_id
            )
        )
        return int(result.scalar_one()) + 1

    async def find_conflicting_active_booking(
        self, user_id: str, provider: str, exclude_id: uuid.UUID
    ) -> uuid.UUID | None:
        """Return the id of an existing active booking for this user+provider.

        Used to enforce that a user may not hold two concurrent active bookings
        with the same provider (Requirement 7.3). Excludes ``exclude_id`` (the
        booking being acted on) and only considers active statuses.
        """
        result = await self.session.execute(
            select(Booking.id).where(
                Booking.user_id == user_id,
                Booking.provider == provider,
                Booking.id != exclude_id,
                Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            )
        )
        return result.scalars().first()

    async def list_events(self, booking_id: uuid.UUID) -> list[BookingEvent]:
        """Return a booking's events ordered by sequence then created_at."""
        result = await self.session.execute(
            select(BookingEvent)
            .where(BookingEvent.booking_id == booking_id)
            .order_by(BookingEvent.sequence.asc(), BookingEvent.created_at.asc())
        )
        return list(result.scalars().all())

    async def list_active_booking_ids(self) -> list[uuid.UUID]:
        """Return ids of confirmed/active bookings that have a provider booking id.

        These are the bookings the ride tracker polls each cycle.
        """
        result = await self.session.execute(
            select(Booking.id).where(
                Booking.status.in_(ACTIVE_BOOKING_STATUSES),
                Booking.provider_booking_id.isnot(None),
            )
        )
        return [row[0] for row in result.all()]

    async def flush(self) -> None:
        """Flush pending changes so server defaults / ids are populated."""
        await self.session.flush()

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self.session.commit()

    async def rollback(self) -> None:
        """Roll back the current transaction."""
        await self.session.rollback()

    async def refresh(self, instance: object) -> None:
        """Refresh an ORM instance from the database."""
        await self.session.refresh(instance)
