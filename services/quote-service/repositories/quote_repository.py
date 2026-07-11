"""Data-access layer for the Quote Service.

The :class:`QuoteRepository` is the ONLY place that issues database queries for
quote sessions, quotes, and quote events. It contains no business rules — just
reads, writes, and transaction control over a single :class:`AsyncSession`. The
service and the monitor worker compose these methods to implement use cases.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.quote_models import (
    Quote,
    QuoteEvent,
    QuoteSession,
    QuoteSessionStatus,
)


class QuoteRepository:
    """Database access for quote sessions, quotes, and events over one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to an active session.

        Args:
            session: The AsyncSession this repository reads and writes through.
        """
        self.session = session

    async def get_session_by_id(
        self, session_id: uuid.UUID
    ) -> QuoteSession | None:
        """Return the quote session with this id, or None when absent."""
        return await self.session.get(QuoteSession, session_id)

    async def get_quote_by_id(self, quote_id: uuid.UUID) -> Quote | None:
        """Return the quote with this id, or None when absent."""
        return await self.session.get(Quote, quote_id)

    def add(self, instance: object) -> None:
        """Stage a new ORM instance (QuoteSession/Quote/QuoteEvent) for insertion."""
        self.session.add(instance)

    async def next_event_sequence(self, session_id: uuid.UUID) -> int:
        """Return the next per-session event sequence number (max existing + 1).

        Events are numbered per session starting at 1 so their order is stable
        and independent of timestamp resolution.
        """
        result = await self.session.execute(
            select(func.coalesce(func.max(QuoteEvent.sequence), 0)).where(
                QuoteEvent.quote_session_id == session_id
            )
        )
        return int(result.scalar_one()) + 1

    async def list_quotes(self, session_id: uuid.UUID) -> list[Quote]:
        """Return all quotes currently stored for a session."""
        result = await self.session.execute(
            select(Quote).where(Quote.quote_session_id == session_id)
        )
        return list(result.scalars().all())

    async def delete_quotes(self, session_id: uuid.UUID) -> None:
        """Delete all stored quotes for a session (before storing a fresh fetch)."""
        await self.session.execute(
            delete(Quote).where(Quote.quote_session_id == session_id)
        )

    async def list_monitoring_sessions(self) -> list[QuoteSession]:
        """Return all sessions currently in MONITORING status."""
        result = await self.session.execute(
            select(QuoteSession).where(
                QuoteSession.status == QuoteSessionStatus.MONITORING.value
            )
        )
        return list(result.scalars().all())

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
