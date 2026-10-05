"""Unit tests for quote-session service workflows."""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.exceptions import MissingCoordinatesError, SessionCancelledError
from models.quote_models import Quote, QuoteEvent, QuoteSession, QuoteSessionStatus
from provider.provider import NormalizedQuote, ProviderError
from schemas.quote_schemas import CreateSessionRequest, SelectQuoteRequest
from services.quote_service import QuoteService


class FakeQuoteRepository:
    """Minimal async repository double that preserves service-visible state."""

    def __init__(self) -> None:
        self.sessions: dict[uuid.UUID, QuoteSession] = {}
        self.quotes: dict[uuid.UUID, Quote] = {}
        self.events: list[QuoteEvent] = []
        self.pending: list[object] = []
        self.deleted_session_ids: list[uuid.UUID] = []
        self.commits = 0

    def add(self, instance: object) -> None:
        self.pending.append(instance)

    async def flush(self) -> None:
        now = datetime.now(UTC)
        for instance in self.pending:
            if isinstance(instance, QuoteSession):
                if instance.id is None:
                    instance.id = uuid.uuid4()
                if instance.created_at is None:
                    instance.created_at = now
                if instance.updated_at is None:
                    instance.updated_at = now
                self.sessions[instance.id] = instance
            elif isinstance(instance, Quote):
                if instance.id is None:
                    instance.id = uuid.uuid4()
                self.quotes[instance.id] = instance
            elif isinstance(instance, QuoteEvent):
                self.events.append(instance)
        self.pending.clear()

    async def commit(self) -> None:
        await self.flush()
        self.commits += 1

    async def refresh(self, instance: object) -> None:
        return None

    async def get_session_by_id(self, session_id: uuid.UUID) -> QuoteSession | None:
        return self.sessions.get(session_id)

    async def get_quote_by_id(self, quote_id: uuid.UUID) -> Quote | None:
        return self.quotes.get(quote_id)

    async def list_quotes(self, session_id: uuid.UUID) -> list[Quote]:
        return [q for q in self.quotes.values() if q.quote_session_id == session_id]

    async def delete_quotes(self, session_id: uuid.UUID) -> None:
        self.deleted_session_ids.append(session_id)
        self.quotes = {
            quote_id: quote
            for quote_id, quote in self.quotes.items()
            if quote.quote_session_id != session_id
        }

    async def next_event_sequence(self, session_id: uuid.UUID) -> int:
        return 1 + sum(event.quote_session_id == session_id for event in self.events)


class StubAdapter:
    def __init__(self, provider: str, result: object) -> None:
        self.provider = provider
        self.result = result

    async def fetch_quotes(self, **_coords: float) -> list[NormalizedQuote]:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class QuoteServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = FakeQuoteRepository()
        self.service = QuoteService(self.repository)

    async def _create_session(self, **overrides: object) -> QuoteSession:
        values = {
            "user_id": "user-123",
            "pickup_address": "Pickup",
            "pickup_lat": 37.77,
            "pickup_lng": -122.42,
            "dropoff_address": "Dropoff",
            "dropoff_lat": 37.78,
            "dropoff_lng": -122.41,
        }
        values.update(overrides)
        return await self.service.create_session(CreateSessionRequest(**values))

    async def test_create_session_persists_session_created_event(self) -> None:
        session = await self._create_session()

        self.assertEqual(session.status, QuoteSessionStatus.CREATED.value)
        self.assertEqual(self.repository.commits, 1)
        self.assertEqual(len(self.repository.events), 1)
        self.assertEqual(self.repository.events[0].event_type, "SESSION_CREATED")
        self.assertEqual(self.repository.events[0].sequence, 1)

    async def test_fetch_quotes_stores_successes_and_reports_provider_failure(self) -> None:
        session = await self._create_session()
        adapters = [
            StubAdapter(
                "uber",
                [
                    NormalizedQuote(
                        provider="uber",
                        ride_type="UberX",
                        price=24.8,
                        pickup_eta_minutes=6,
                        trip_duration_minutes=22,
                    )
                ],
            ),
            StubAdapter("lyft", ProviderError("lyft", "temporarily unavailable")),
        ]

        with patch("services.quote_service.get_all_adapters", return_value=adapters):
            response = await self.service.fetch_quotes(session.id)

        self.assertEqual(response.session.status, QuoteSessionStatus.MONITORING.value)
        self.assertEqual([quote.provider for quote in response.quotes], ["uber"])
        self.assertEqual(response.unavailable_providers, ["lyft"])
        self.assertEqual(self.repository.deleted_session_ids, [session.id])
        self.assertEqual(
            [event.event_type for event in self.repository.events],
            ["SESSION_CREATED", "PROVIDER_UNAVAILABLE", "QUOTE_SNAPSHOT"],
        )

    async def test_fetch_quotes_requires_coordinates_and_active_session(self) -> None:
        missing_coordinates = await self._create_session(pickup_lat=None)
        with self.assertRaises(MissingCoordinatesError):
            await self.service.fetch_quotes(missing_coordinates.id)

        cancelled = await self._create_session()
        cancelled.status = QuoteSessionStatus.CANCELLED.value
        with self.assertRaises(SessionCancelledError):
            await self.service.fetch_quotes(cancelled.id)

    async def test_select_quote_marks_session_and_records_event(self) -> None:
        session = await self._create_session()
        quote = Quote(
            id=uuid.uuid4(),
            quote_session_id=session.id,
            provider="uber",
            ride_type="UberX",
            price=24.8,
            currency="USD",
            pickup_eta_minutes=6,
            trip_duration_minutes=22,
            available=True,
        )
        self.repository.quotes[quote.id] = quote

        response = await self.service.select_quote(
            session.id, SelectQuoteRequest(quote_id=quote.id)
        )

        self.assertEqual(response.session.status, QuoteSessionStatus.QUOTE_SELECTED.value)
        self.assertEqual(response.selected_quote.id, quote.id)
        self.assertEqual(self.repository.events[-1].event_type, "QUOTE_SELECTED")


if __name__ == "__main__":
    unittest.main()
