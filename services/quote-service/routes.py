"""Quote session endpoints for the Quote Service.

Exposes the session lifecycle the AI Service drives:

    POST /quotes/sessions              create a session
    POST /quotes/sessions/{id}/fetch   fetch + store quotes from all providers
    POST /quotes/sessions/{id}/select  record a selection
    POST /quotes/sessions/{id}/cancel  cancel a session

All provider calls go through the adapter layer (provider_adapter.py) and are
fanned out in parallel; a single failing provider is skipped and recorded as a
PROVIDER_UNAVAILABLE event rather than failing the whole fetch. Every meaningful
action is recorded as a quote_event so a session's history is captured end to
end.

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
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from models import Quote, QuoteEvent, QuoteEventType, QuoteSession, QuoteSessionStatus
from provider_adapter import NormalizedQuote, ProviderError, get_all_adapters
from schemas import (
    CreateSessionRequest,
    FetchQuotesResponse,
    QuoteSessionOut,
    SelectQuoteRequest,
    SelectQuoteResponse,
    SessionStateResponse,
)

router = APIRouter(prefix="/quotes", tags=["quotes"])


async def _get_session_or_404(
    session_id: uuid.UUID, db: AsyncSession
) -> QuoteSession:
    """Load a quote session by id or raise 404.

    Args:
        session_id: The quote session id from the path.
        db: Active database session.

    Returns:
        The QuoteSession ORM object.

    Raises:
        HTTPException: 404 if no session with that id exists.
    """
    session = await db.get(QuoteSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Quote session not found")
    return session


async def _next_sequence(session_id: uuid.UUID, db: AsyncSession) -> int:
    """Return the next event sequence number for a session.

    Events are numbered per session starting at 1 so their order is stable and
    independent of timestamp resolution.

    Args:
        session_id: The quote session the event belongs to.
        db: Active database session.

    Returns:
        The next sequence integer (max existing + 1, or 1 if none yet).
    """
    result = await db.execute(
        select(func.coalesce(func.max(QuoteEvent.sequence), 0)).where(
            QuoteEvent.quote_session_id == session_id
        )
    )
    return int(result.scalar_one()) + 1


@router.post("/sessions", response_model=QuoteSessionOut, status_code=201)
async def create_session(
    req: CreateSessionRequest, db: AsyncSession = Depends(get_session)
) -> QuoteSession:
    """Create a new quote session.

    Persists the pickup/dropoff context the AI Service resolved and records a
    SESSION_CREATED event. The session starts in CREATED status; quotes are
    fetched separately via the /fetch endpoint. (Requirement 1.4)

    Args:
        req: The session details (user, chat session, pickup/dropoff).
        db: Active database session.

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
    db.add(session)
    await db.flush()

    db.add(
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

    await db.commit()
    await db.refresh(session)
    return session


@router.get("/sessions/{session_id}", response_model=SessionStateResponse)
async def get_session_state(
    session_id: uuid.UUID, db: AsyncSession = Depends(get_session)
) -> SessionStateResponse:
    """Return a read-only snapshot of a session's current state and quotes.

    Lets the AI Service read the authoritative pickup/dropoff, status, and any
    stored quotes for a session so it can build a ride-state summary for the
    model each turn (rather than the model re-asking for details it already
    has). Does not mutate the session or trigger a fetch.

    Args:
        session_id: The session to read.
        db: Active database session.

    Returns:
        SessionStateResponse with the session and its currently stored quotes.

    Raises:
        HTTPException: 404 if the session does not exist.
    """
    session = await _get_session_or_404(session_id, db)
    result = await db.execute(
        select(Quote).where(Quote.quote_session_id == session_id)
    )
    quotes = list(result.scalars().all())
    return SessionStateResponse(
        session=QuoteSessionOut.model_validate(session),
        quotes=quotes,
    )


@router.post("/sessions/{session_id}/fetch", response_model=FetchQuotesResponse)
async def fetch_quotes(
    session_id: uuid.UUID, db: AsyncSession = Depends(get_session)
) -> FetchQuotesResponse:
    """Fetch quotes from all providers in parallel and store them.

    Fans out to every provider adapter concurrently with asyncio.gather. A
    provider that fails (ProviderError) is skipped and recorded as a
    PROVIDER_UNAVAILABLE event; the remaining providers' quotes are still stored
    so the caller gets partial results rather than an error. Previously stored
    quotes for the session are cleared so the stored set always reflects the
    latest fetch. On success the session moves to MONITORING and a
    QUOTE_SNAPSHOT event captures the full set. (Requirements 2.1, 2.2)

    Args:
        session_id: The session to fetch quotes for.
        db: Active database session.

    Returns:
        FetchQuotesResponse with the session, stored quotes, and any providers
        that were unavailable.

    Raises:
        HTTPException: 404 if the session does not exist, 409 if it is cancelled,
            or 400 if pickup/dropoff coordinates are not yet set.
    """
    session = await _get_session_or_404(session_id, db)

    if session.status == QuoteSessionStatus.CANCELLED.value:
        raise HTTPException(
            status_code=409, detail="Cannot fetch quotes for a cancelled session"
        )

    if (
        session.pickup_lat is None
        or session.pickup_lng is None
        or session.dropoff_lat is None
        or session.dropoff_lng is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Pickup and dropoff coordinates are required to fetch quotes",
        )

    session.status = QuoteSessionStatus.FETCHING_QUOTES.value
    await db.flush()

    coords = {
        "pickup_lat": float(session.pickup_lat),
        "pickup_lng": float(session.pickup_lng),
        "dropoff_lat": float(session.dropoff_lat),
        "dropoff_lng": float(session.dropoff_lng),
    }

    # Fan out to every provider in parallel over a shared HTTP client. Exceptions
    # are captured (return_exceptions=True) so one failing provider can't cancel
    # the others — we triage each result below.
    async with httpx.AsyncClient(timeout=10.0) as client:
        adapters = get_all_adapters(client=client)
        results = await asyncio.gather(
            *(adapter.fetch_quotes(**coords) for adapter in adapters),
            return_exceptions=True,
        )

    stored_quotes: list[Quote] = []
    unavailable: list[str] = []

    # Clear any quotes from a prior fetch so the stored set reflects this fetch.
    await db.execute(
        delete(Quote).where(Quote.quote_session_id == session.id)
    )
    await db.flush()

    sequence = await _next_sequence(session_id, db)

    for adapter, result in zip(adapters, results):
        if isinstance(result, ProviderError):
            unavailable.append(adapter.provider)
            db.add(
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
            # Any unexpected (non-ProviderError) failure is also treated as the
            # provider being unavailable rather than failing the whole fetch.
            unavailable.append(adapter.provider)
            db.add(
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
            db.add(quote)
            stored_quotes.append(quote)

    await db.flush()

    # Record a snapshot of the full fetched set. Move to MONITORING so the
    # background worker can begin refreshing prices for this session.
    db.add(
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
    await db.commit()

    await db.refresh(session)
    for q in stored_quotes:
        await db.refresh(q)

    return FetchQuotesResponse(
        session=QuoteSessionOut.model_validate(session),
        quotes=stored_quotes,
        unavailable_providers=unavailable,
    )


@router.post("/sessions/{session_id}/select", response_model=SelectQuoteResponse)
async def select_quote(
    session_id: uuid.UUID,
    req: SelectQuoteRequest,
    db: AsyncSession = Depends(get_session),
) -> SelectQuoteResponse:
    """Record the user's selection of a specific quote.

    Sets the session status to QUOTE_SELECTED and records a QUOTE_SELECTED event
    referencing the chosen quote. Monitoring continues after selection — a later
    price change on the selected quote requires re-confirmation (handled by the
    Booking Service). (Requirement 4.1)

    Args:
        session_id: The session the selection belongs to.
        req: The selection request carrying the chosen quote_id.
        db: Active database session.

    Returns:
        SelectQuoteResponse with the updated session and the selected quote.

    Raises:
        HTTPException: 404 if the session or quote does not exist, 409 if the
            session is cancelled, or 400 if the quote belongs to another session.
    """
    session = await _get_session_or_404(session_id, db)

    if session.status == QuoteSessionStatus.CANCELLED.value:
        raise HTTPException(
            status_code=409, detail="Cannot select a quote on a cancelled session"
        )

    quote = await db.get(Quote, req.quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    if quote.quote_session_id != session.id:
        raise HTTPException(
            status_code=400,
            detail="Quote does not belong to this session",
        )

    session.status = QuoteSessionStatus.QUOTE_SELECTED.value
    await db.flush()

    sequence = await _next_sequence(session_id, db)
    db.add(
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

    await db.commit()
    await db.refresh(session)
    await db.refresh(quote)

    return SelectQuoteResponse(
        session=QuoteSessionOut.model_validate(session),
        selected_quote=quote,
    )


@router.post("/sessions/{session_id}/cancel", response_model=QuoteSessionOut)
async def cancel_session(
    session_id: uuid.UUID, db: AsyncSession = Depends(get_session)
) -> QuoteSession:
    """Cancel a quote session.

    Marks the session CANCELLED and records a SESSION_CANCELLED event, which
    stops it from being picked up by the monitoring worker. Cancelling an
    already-cancelled session is a no-op and returns the current state.

    Args:
        session_id: The session to cancel.
        db: Active database session.

    Returns:
        The cancelled QuoteSession.

    Raises:
        HTTPException: 404 if the session does not exist.
    """
    session = await _get_session_or_404(session_id, db)

    if session.status == QuoteSessionStatus.CANCELLED.value:
        return session

    previous_status = session.status
    session.status = QuoteSessionStatus.CANCELLED.value
    await db.flush()

    sequence = await _next_sequence(session_id, db)
    db.add(
        QuoteEvent(
            quote_session_id=session.id,
            event_type=QuoteEventType.SESSION_CANCELLED.value,
            sequence=sequence,
            payload={"previous_status": previous_status},
        )
    )

    await db.commit()
    await db.refresh(session)
    return session
