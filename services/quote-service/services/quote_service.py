"""Business logic for the Quote Service.

The :class:`QuoteService` implements the quote session lifecycle the AI Service
drives: create a session, read its state, fetch quotes from all providers in
parallel, record a selection, and cancel. It orchestrates the repository (DB) and
the provider adapters (upstream calls) and owns the transaction for each use case.

It is deliberately HTTP-agnostic: on failure it raises the domain exceptions in
core.exceptions, which the API layer maps to HTTP status codes.

Requirements:
  1.4 — create a quote session once pickup and dropoff are known.
  2.1 — fetch quotes from all providers in parallel.
  2.2 — store quotes normalized into the common shape.
  4.1 — record a selection and set status to QUOTE_SELECTED.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from core.exceptions import (
    MissingCoordinatesError,
    QuoteNotFoundError,
    QuoteNotInSessionError,
    QuoteSessionNotFoundError,
    SessionCancelledError,
)
from models.quote_models import (
    Quote,
    QuoteEvent,
    QuoteEventType,
    QuoteSession,
    QuoteSessionStatus,
)
from provider.provider import NormalizedQuote, ProviderError, get_all_adapters
from repositories.quote_repository import QuoteRepository
from schemas.quote_schemas import (
    CreateSessionRequest,
    FetchQuotesResponse,
    QuoteSessionOut,
    SelectQuoteRequest,
    SelectQuoteResponse,
    SessionStateResponse,
)

# Per-request timeout (seconds) for the parallel provider fan-out during a fetch.
FETCH_TIMEOUT_SECONDS = 10.0


class QuoteService:
    """Coordinates the repository and provider adapters to run quote use cases."""

    def __init__(self, repository: QuoteRepository) -> None:
        """Wire the service to its data-access dependency.

        Args:
            repository: Data-access layer for quote sessions, quotes, and events.
        """
        self.repository = repository

    async def create_session(self, req: CreateSessionRequest) -> QuoteSession:
        """Create a new quote session and record a SESSION_CREATED event.

        The session starts in CREATED status; quotes are fetched separately via
        :meth:`fetch_quotes`. (Requirement 1.4)

        Args:
            req: The session details (user, chat session, pickup/dropoff).

        Returns:
            The newly created QuoteSession.
        """
        session = QuoteSession(
            user_id=req.user_id,
            chat_session_id=req.chat_session_id,
            pickup_address=req.pickup_address,
            pickup_lat=req.pickup_lat,
            pickup_lng=req.pickup_lng,
            dropoff_address=req.dropoff_address,
            dropoff_lat=req.dropoff_lat,
            dropoff_lng=req.dropoff_lng,
            status=QuoteSessionStatus.CREATED.value,
        )
        self.repository.add(session)
        await self.repository.flush()

        self.repository.add(
            QuoteEvent(
                quote_session_id=session.id,
                event_type=QuoteEventType.SESSION_CREATED.value,
                sequence=1,
                payload={
                    "user_id": req.user_id,
                    "pickup_address": req.pickup_address,
                    "dropoff_address": req.dropoff_address,
                },
            )
        )

        await self.repository.commit()
        await self.repository.refresh(session)
        return session

    async def get_session_state(
        self, session_id: uuid.UUID
    ) -> SessionStateResponse:
        """Return a read-only snapshot of a session's state and stored quotes.

        Args:
            session_id: The session to read.

        Returns:
            SessionStateResponse with the session and its currently stored quotes.

        Raises:
            QuoteSessionNotFoundError: If the session does not exist.
        """
        session = await self._get_session_or_raise(session_id)
        quotes = await self.repository.list_quotes(session_id)
        return SessionStateResponse(
            session=QuoteSessionOut.model_validate(session),
            quotes=quotes,
        )

    async def fetch_quotes(self, session_id: uuid.UUID) -> FetchQuotesResponse:
        """Fetch quotes from all providers in parallel and store them.

        Fans out to every provider adapter concurrently. A provider that fails is
        skipped and recorded as a PROVIDER_UNAVAILABLE event; the remaining
        providers' quotes are still stored so the caller gets partial results.
        Previously stored quotes for the session are cleared so the stored set
        always reflects the latest fetch. On success the session moves to
        MONITORING and a QUOTE_SNAPSHOT event captures the full set.
        (Requirements 2.1, 2.2)

        Args:
            session_id: The session to fetch quotes for.

        Returns:
            FetchQuotesResponse with the session, stored quotes, and any providers
            that were unavailable.

        Raises:
            QuoteSessionNotFoundError: If the session does not exist.
            SessionCancelledError: If the session is cancelled.
            MissingCoordinatesError: If pickup/dropoff coordinates are not set.
        """
        session = await self._get_session_or_raise(session_id)

        if session.status == QuoteSessionStatus.CANCELLED.value:
            raise SessionCancelledError(
                "Cannot fetch quotes for a cancelled session"
            )

        if (
            session.pickup_lat is None
            or session.pickup_lng is None
            or session.dropoff_lat is None
            or session.dropoff_lng is None
        ):
            raise MissingCoordinatesError(
                "Pickup and dropoff coordinates are required to fetch quotes"
            )

        session.status = QuoteSessionStatus.FETCHING_QUOTES.value
        await self.repository.flush()

        coords = {
            "pickup_lat": float(session.pickup_lat),
            "pickup_lng": float(session.pickup_lng),
            "dropoff_lat": float(session.dropoff_lat),
            "dropoff_lng": float(session.dropoff_lng),
        }

        # Fan out to every provider in parallel over a shared HTTP client.
        # Exceptions are captured (return_exceptions=True) so one failing provider
        # can't cancel the others — each result is triaged below.
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
            adapters = get_all_adapters(client=client)
            results = await asyncio.gather(
                *(adapter.fetch_quotes(**coords) for adapter in adapters),
                return_exceptions=True,
            )

        stored_quotes: list[Quote] = []
        unavailable: list[str] = []

        # Clear any quotes from a prior fetch so the stored set reflects this one.
        await self.repository.delete_quotes(session.id)
        await self.repository.flush()

        sequence = await self.repository.next_event_sequence(session_id)

        for adapter, result in zip(adapters, results):
            if isinstance(result, ProviderError):
                unavailable.append(adapter.provider)
                self.repository.add(
                    QuoteEvent(
                        quote_session_id=session.id,
                        event_type=QuoteEventType.PROVIDER_UNAVAILABLE.value,
                        sequence=sequence,
                        payload={
                            "provider": adapter.provider,
                            "error": result.message,
                        },
                    )
                )
                sequence += 1
                continue
            if isinstance(result, BaseException):
                # Any unexpected (non-ProviderError) failure is also treated as
                # the provider being unavailable rather than failing the fetch.
                unavailable.append(adapter.provider)
                self.repository.add(
                    QuoteEvent(
                        quote_session_id=session.id,
                        event_type=QuoteEventType.PROVIDER_UNAVAILABLE.value,
                        sequence=sequence,
                        payload={
                            "provider": adapter.provider,
                            "error": str(result),
                        },
                    )
                )
                sequence += 1
                continue

            normalized: list[NormalizedQuote] = result
            for nq in normalized:
                quote = Quote(
                    quote_session_id=session.id,
                    provider=nq.provider,
                    ride_type=nq.ride_type,
                    price=nq.price,
                    currency=nq.currency,
                    pickup_eta_minutes=nq.pickup_eta_minutes,
                    trip_duration_minutes=nq.trip_duration_minutes,
                    available=nq.available,
                )
                self.repository.add(quote)
                stored_quotes.append(quote)

        await self.repository.flush()

        # Record a snapshot of the full fetched set. Move to MONITORING so the
        # background worker can begin refreshing prices for this session.
        self.repository.add(
            QuoteEvent(
                quote_session_id=session.id,
                event_type=QuoteEventType.QUOTE_SNAPSHOT.value,
                sequence=sequence,
                payload={
                    "quotes": [
                        {
                            "id": str(q.id),
                            "provider": q.provider,
                            "ride_type": q.ride_type,
                            "price": float(q.price),
                            "currency": q.currency,
                            "pickup_eta_minutes": q.pickup_eta_minutes,
                            "trip_duration_minutes": q.trip_duration_minutes,
                            "available": q.available,
                        }
                        for q in stored_quotes
                    ],
                    "unavailable_providers": unavailable,
                },
            )
        )

        session.status = QuoteSessionStatus.MONITORING.value
        await self.repository.commit()

        await self.repository.refresh(session)
        for q in stored_quotes:
            await self.repository.refresh(q)

        return FetchQuotesResponse(
            session=QuoteSessionOut.model_validate(session),
            quotes=stored_quotes,
            unavailable_providers=unavailable,
        )

    async def select_quote(
        self, session_id: uuid.UUID, req: SelectQuoteRequest
    ) -> SelectQuoteResponse:
        """Record the user's selection of a specific quote.

        Sets the session status to QUOTE_SELECTED and records a QUOTE_SELECTED
        event referencing the chosen quote. Monitoring continues after selection.
        (Requirement 4.1)

        Args:
            session_id: The session the selection belongs to.
            req: The selection request carrying the chosen quote_id.

        Returns:
            SelectQuoteResponse with the updated session and the selected quote.

        Raises:
            QuoteSessionNotFoundError: If the session does not exist.
            SessionCancelledError: If the session is cancelled.
            QuoteNotFoundError: If the quote does not exist.
            QuoteNotInSessionError: If the quote belongs to another session.
        """
        session = await self._get_session_or_raise(session_id)

        if session.status == QuoteSessionStatus.CANCELLED.value:
            raise SessionCancelledError(
                "Cannot select a quote on a cancelled session"
            )

        quote = await self.repository.get_quote_by_id(req.quote_id)
        if quote is None:
            raise QuoteNotFoundError(req.quote_id)
        if quote.quote_session_id != session.id:
            raise QuoteNotInSessionError("Quote does not belong to this session")

        session.status = QuoteSessionStatus.QUOTE_SELECTED.value
        await self.repository.flush()

        sequence = await self.repository.next_event_sequence(session_id)
        self.repository.add(
            QuoteEvent(
                quote_session_id=session.id,
                quote_id=quote.id,
                event_type=QuoteEventType.QUOTE_SELECTED.value,
                sequence=sequence,
                payload={
                    "quote_id": str(quote.id),
                    "provider": quote.provider,
                    "ride_type": quote.ride_type,
                    "price": float(quote.price),
                    "currency": quote.currency,
                },
            )
        )

        await self.repository.commit()
        await self.repository.refresh(session)
        await self.repository.refresh(quote)

        return SelectQuoteResponse(
            session=QuoteSessionOut.model_validate(session),
            selected_quote=quote,
        )

    async def cancel_session(self, session_id: uuid.UUID) -> QuoteSession:
        """Cancel a quote session, stopping it from being monitored.

        Marks the session CANCELLED and records a SESSION_CANCELLED event.
        Cancelling an already-cancelled session is a no-op that returns the
        current state.

        Args:
            session_id: The session to cancel.

        Returns:
            The cancelled QuoteSession.

        Raises:
            QuoteSessionNotFoundError: If the session does not exist.
        """
        session = await self._get_session_or_raise(session_id)

        if session.status == QuoteSessionStatus.CANCELLED.value:
            return session

        previous_status = session.status
        session.status = QuoteSessionStatus.CANCELLED.value
        await self.repository.flush()

        sequence = await self.repository.next_event_sequence(session_id)
        self.repository.add(
            QuoteEvent(
                quote_session_id=session.id,
                event_type=QuoteEventType.SESSION_CANCELLED.value,
                sequence=sequence,
                payload={"previous_status": previous_status},
            )
        )

        await self.repository.commit()
        await self.repository.refresh(session)
        return session

    async def _get_session_or_raise(
        self, session_id: uuid.UUID
    ) -> QuoteSession:
        """Load a session or raise QuoteSessionNotFoundError."""
        session = await self.repository.get_session_by_id(session_id)
        if session is None:
            raise QuoteSessionNotFoundError(session_id)
        return session
