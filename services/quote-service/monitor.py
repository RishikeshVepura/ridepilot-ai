"""Quote monitoring worker for the Quote Service.

Runs as an asyncio background task started on service startup (see main.py's
lifespan). On a configurable interval it:

    1. Loads every quote session in MONITORING status.
    2. Fetches fresh quotes from all providers via the adapter layer.
    3. Compares the fresh quotes against the session's last stored snapshot.
    4. If anything changed, stores the new snapshot (updating the persisted
       quotes in place and recording a QUOTE_DELTA quote_event) and POSTs a
       QUOTE_DELTA event to the AI Service's /internal/events endpoint.
    5. If nothing changed, does nothing for that session.

The refresh interval is read from QUOTE_REFRESH_INTERVAL_SECONDS (default 60s)
and the AI Service location from AI_SERVICE_URL, keeping both configurable and
consistent with the rest of the codebase.

The price/ETA comparison core (``compute_changes``) is kept pure and free of any
database or HTTP concerns so it can be unit and property tested in isolation.

Requirements:
  3.1 — refresh MONITORING sessions on a configurable interval (default 60s).
  3.2 — on change, store the new snapshot and publish QUOTE_DELTA to AI Service.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import async_session_factory
from models import Quote, QuoteEvent, QuoteEventType, QuoteSession, QuoteSessionStatus
from provider_adapter import NormalizedQuote, ProviderError, get_all_adapters

logger = logging.getLogger("quote-service.monitor")

# Default refresh cadence in seconds when QUOTE_REFRESH_INTERVAL_SECONDS is unset.
DEFAULT_REFRESH_INTERVAL_SECONDS = 60.0

# Base URL of the AI Service that consumes background events. Inside the Docker
# network this resolves to the compose service name; overridable via env.
AI_SERVICE_URL = os.getenv("AI_SERVICE_URL", "http://ai-service:8001")

# Path on the AI Service that receives Quote/Booking background events.
INTERNAL_EVENTS_PATH = "/internal/events"

# Per-request timeout (seconds) for posting events to the AI Service.
EVENT_POST_TIMEOUT_SECONDS = 10.0


def get_refresh_interval_seconds() -> float:
    """Return the monitoring refresh interval in seconds.

    Reads QUOTE_REFRESH_INTERVAL_SECONDS and falls back to the default when the
    variable is unset or not a positive number.

    Returns:
        The interval in seconds (always > 0).
    """
    raw = os.getenv("QUOTE_REFRESH_INTERVAL_SECONDS")
    if raw is None:
        return DEFAULT_REFRESH_INTERVAL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid QUOTE_REFRESH_INTERVAL_SECONDS=%r; using default %ss",
            raw,
            DEFAULT_REFRESH_INTERVAL_SECONDS,
        )
        return DEFAULT_REFRESH_INTERVAL_SECONDS
    if value <= 0:
        logger.warning(
            "Non-positive QUOTE_REFRESH_INTERVAL_SECONDS=%r; using default %ss",
            raw,
            DEFAULT_REFRESH_INTERVAL_SECONDS,
        )
        return DEFAULT_REFRESH_INTERVAL_SECONDS
    return value


@dataclass(frozen=True)
class SnapshotQuote:
    """A provider-agnostic view of one stored quote, used for comparison.

    Decouples the pure comparison logic from the SQLAlchemy ORM so it can be
    tested without a database. Mirrors the comparable fields of a NormalizedQuote.

    Fields:
        provider: Provider key (e.g. "uber").
        ride_type: Ride type name (e.g. "UberX").
        price: Quoted price.
        pickup_eta_minutes: Estimated minutes until pickup, if known.
        available: Whether the option is currently bookable.
    """

    provider: str
    ride_type: str
    price: float
    pickup_eta_minutes: int | None = None
    available: bool = True


def _round_price(price: float | None) -> float | None:
    """Round a price to 2 decimal places, preserving None.

    Comparing rounded prices avoids spurious deltas from floating-point noise
    when the underlying value is effectively unchanged.
    """
    if price is None:
        return None
    return round(float(price), 2)


def compute_changes(
    existing: list[SnapshotQuote], fresh: list[NormalizedQuote]
) -> list[dict]:
    """Compare a stored snapshot to freshly fetched quotes and list the changes.

    Quotes are matched by (provider, ride_type). A change is reported when the
    price, the pickup ETA, or availability differs, when a new (provider,
    ride_type) appears, or when a previously stored one disappears.

    This function is pure: it performs no I/O and has no side effects, so it can
    be exercised directly by unit and property tests.

    Args:
        existing: The last stored snapshot for the session.
        fresh: The freshly fetched, normalized quotes to compare against. Should
            only contain providers that were successfully fetched this cycle.

    Returns:
        A list of change dicts, each matching the QUOTE_DELTA change shape:
        provider, ride_type, old_price, new_price, old_pickup_eta_minutes,
        new_pickup_eta_minutes. Added quotes have old_* set to None; removed
        quotes have new_* set to None. Empty when nothing changed.
    """
    existing_by_key = {(q.provider, q.ride_type): q for q in existing}
    fresh_by_key = {(q.provider, q.ride_type): q for q in fresh}

    changes: list[dict] = []

    # Matched and newly-appeared quotes.
    for key, new_q in fresh_by_key.items():
        old_q = existing_by_key.get(key)
        new_price = _round_price(new_q.price)
        new_eta = new_q.pickup_eta_minutes

        if old_q is None:
            # A ride type we did not have before.
            changes.append(
                {
                    "provider": new_q.provider,
                    "ride_type": new_q.ride_type,
                    "old_price": None,
                    "new_price": new_price,
                    "old_pickup_eta_minutes": None,
                    "new_pickup_eta_minutes": new_eta,
                }
            )
            continue

        old_price = _round_price(old_q.price)
        old_eta = old_q.pickup_eta_minutes
        if (
            old_price != new_price
            or old_eta != new_eta
            or old_q.available != new_q.available
        ):
            changes.append(
                {
                    "provider": new_q.provider,
                    "ride_type": new_q.ride_type,
                    "old_price": old_price,
                    "new_price": new_price,
                    "old_pickup_eta_minutes": old_eta,
                    "new_pickup_eta_minutes": new_eta,
                }
            )

    # Quotes that disappeared from the fresh fetch.
    for key, old_q in existing_by_key.items():
        if key not in fresh_by_key:
            changes.append(
                {
                    "provider": old_q.provider,
                    "ride_type": old_q.ride_type,
                    "old_price": _round_price(old_q.price),
                    "new_price": None,
                    "old_pickup_eta_minutes": old_q.pickup_eta_minutes,
                    "new_pickup_eta_minutes": None,
                }
            )

    return changes


async def _next_sequence(session_id: uuid.UUID, db: AsyncSession) -> int:
    """Return the next per-session event sequence number (max existing + 1)."""
    result = await db.execute(
        select(func.coalesce(func.max(QuoteEvent.sequence), 0)).where(
            QuoteEvent.quote_session_id == session_id
        )
    )
    return int(result.scalar_one()) + 1


async def _fetch_fresh_quotes(
    session: QuoteSession, client: httpx.AsyncClient
) -> tuple[list[NormalizedQuote], set[str]]:
    """Fetch fresh quotes for a session from every provider in parallel.

    Mirrors the fetch endpoint's fan-out: a provider that fails is skipped
    (its last snapshot is left untouched) rather than failing the whole cycle.

    Args:
        session: The MONITORING session whose pickup/dropoff coords are used.
        client: A shared httpx client for the provider calls.

    Returns:
        A tuple of (fresh quotes, set of providers successfully fetched).
    """
    coords = {
        "pickup_lat": float(session.pickup_lat),
        "pickup_lng": float(session.pickup_lng),
        "dropoff_lat": float(session.dropoff_lat),
        "dropoff_lng": float(session.dropoff_lng),
    }

    adapters = get_all_adapters(client=client)
    results = await asyncio.gather(
        *(adapter.fetch_quotes(**coords) for adapter in adapters),
        return_exceptions=True,
    )

    fresh: list[NormalizedQuote] = []
    fetched_providers: set[str] = set()
    for adapter, result in zip(adapters, results):
        if isinstance(result, ProviderError):
            logger.warning(
                "Provider %s unavailable while monitoring session %s: %s",
                adapter.provider,
                session.id,
                result.message,
            )
            continue
        if isinstance(result, BaseException):
            logger.warning(
                "Unexpected error fetching %s for session %s: %s",
                adapter.provider,
                session.id,
                result,
            )
            continue
        fetched_providers.add(adapter.provider)
        fresh.extend(result)

    return fresh, fetched_providers


def _apply_changes_to_quotes(
    session_quotes: list[Quote],
    fresh: list[NormalizedQuote],
    fetched_providers: set[str],
    db: AsyncSession,
    session_id: uuid.UUID,
) -> None:
    """Update the persisted quotes in place to reflect the fresh snapshot.

    Existing rows are matched by (provider, ride_type) and updated so their ids
    are preserved (a selected quote references its id). Newly appeared quotes are
    inserted; quotes that disappeared from a successfully-fetched provider are
    marked unavailable. Providers that failed this cycle are left untouched.
    """
    fresh_by_key = {(q.provider, q.ride_type): q for q in fresh}
    existing_by_key = {(q.provider, q.ride_type): q for q in session_quotes}

    # Update matched rows and insert newly appeared ones.
    for key, nq in fresh_by_key.items():
        existing = existing_by_key.get(key)
        if existing is None:
            db.add(
                Quote(
                    quote_session_id=session_id,
                    provider=nq.provider,
                    ride_type=nq.ride_type,
                    price=nq.price,
                    currency=nq.currency,
                    pickup_eta_minutes=nq.pickup_eta_minutes,
                    trip_duration_minutes=nq.trip_duration_minutes,
                    available=nq.available,
                )
            )
        else:
            existing.price = nq.price
            existing.currency = nq.currency
            existing.pickup_eta_minutes = nq.pickup_eta_minutes
            existing.trip_duration_minutes = nq.trip_duration_minutes
            existing.available = nq.available

    # Mark quotes that disappeared (only for providers we actually reached).
    for key, existing in existing_by_key.items():
        provider, _ = key
        if provider in fetched_providers and key not in fresh_by_key:
            existing.available = False


async def _post_quote_delta(
    client: httpx.AsyncClient, payload: dict
) -> None:
    """POST a QUOTE_DELTA event to the AI Service, swallowing transport errors.

    The worker must keep running even if the AI Service is briefly unreachable,
    so failures are logged rather than raised. The snapshot has already been
    persisted, so a missed event does not corrupt state.
    """
    url = f"{AI_SERVICE_URL.rstrip('/')}{INTERNAL_EVENTS_PATH}"
    try:
        response = await client.post(
            url, json=payload, timeout=EVENT_POST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "Failed to POST QUOTE_DELTA for session %s to %s: %s",
            payload.get("quote_session_id"),
            url,
            exc,
        )


async def process_session(
    session: QuoteSession, db: AsyncSession, client: httpx.AsyncClient
) -> list[dict]:
    """Refresh one MONITORING session and publish a delta if anything changed.

    Loads the session's stored quotes, fetches fresh ones, computes the changes,
    and — only when there are changes — updates the stored snapshot, records a
    QUOTE_DELTA quote_event, commits, and POSTs the delta to the AI Service.

    Args:
        session: The MONITORING session to refresh.
        db: Active database session.
        client: Shared httpx client for provider and AI Service calls.

    Returns:
        The list of changes detected (empty if nothing changed).
    """
    # Can't fetch without complete coordinates; skip such sessions defensively.
    if (
        session.pickup_lat is None
        or session.pickup_lng is None
        or session.dropoff_lat is None
        or session.dropoff_lng is None
    ):
        return []

    result = await db.execute(
        select(Quote).where(Quote.quote_session_id == session.id)
    )
    session_quotes = list(result.scalars().all())

    fresh, fetched_providers = await _fetch_fresh_quotes(session, client)
    if not fetched_providers:
        # Every provider failed this cycle — nothing reliable to compare against.
        return []

    # Only compare against the providers we actually reached this cycle.
    existing_snapshot = [
        SnapshotQuote(
            provider=q.provider,
            ride_type=q.ride_type,
            price=float(q.price),
            pickup_eta_minutes=q.pickup_eta_minutes,
            available=q.available,
        )
        for q in session_quotes
        if q.provider in fetched_providers
    ]

    changes = compute_changes(existing_snapshot, fresh)
    if not changes:
        return []

    _apply_changes_to_quotes(
        session_quotes, fresh, fetched_providers, db, session.id
    )

    sequence = await _next_sequence(session.id, db)
    delta_payload = {
        "event_type": QuoteEventType.QUOTE_DELTA.value,
        "quote_session_id": str(session.id),
        "chat_session_id": (
            str(session.chat_session_id) if session.chat_session_id else None
        ),
        "user_id": session.user_id,
        "changes": changes,
    }
    db.add(
        QuoteEvent(
            quote_session_id=session.id,
            event_type=QuoteEventType.QUOTE_DELTA.value,
            sequence=sequence,
            payload={"changes": changes},
        )
    )
    await db.commit()

    await _post_quote_delta(client, delta_payload)
    return changes


async def run_monitor_cycle(client: httpx.AsyncClient) -> int:
    """Run a single monitoring pass over all MONITORING sessions.

    Each session is processed in its own database session so one session's
    failure cannot roll back another's snapshot.

    Args:
        client: Shared httpx client for provider and AI Service calls.

    Returns:
        The number of sessions that had changes published.
    """
    async with async_session_factory() as db:
        result = await db.execute(
            select(QuoteSession).where(
                QuoteSession.status == QuoteSessionStatus.MONITORING.value
            )
        )
        sessions = list(result.scalars().all())

    published = 0
    for session in sessions:
        async with async_session_factory() as db:
            # Re-load the session in this unit of work so updates are tracked.
            fresh_session = await db.get(QuoteSession, session.id)
            if (
                fresh_session is None
                or fresh_session.status != QuoteSessionStatus.MONITORING.value
            ):
                continue
            try:
                changes = await process_session(fresh_session, db, client)
                if changes:
                    published += 1
            except Exception:  # noqa: BLE001 - keep the worker alive
                logger.exception(
                    "Error processing session %s during monitoring", session.id
                )
                await db.rollback()

    return published


async def monitor_loop(stop_event: asyncio.Event) -> None:
    """Background loop that refreshes MONITORING sessions on the configured interval.

    Runs until ``stop_event`` is set (on service shutdown). A shared httpx client
    is reused across cycles. Any per-cycle error is logged and the loop continues.

    Args:
        stop_event: Set by the lifespan handler to request a graceful stop.
    """
    interval = get_refresh_interval_seconds()
    logger.info("Quote monitoring worker started (interval=%ss)", interval)

    async with httpx.AsyncClient() as client:
        while not stop_event.is_set():
            try:
                await run_monitor_cycle(client)
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception("Quote monitoring cycle failed")

            # Sleep for the interval, but wake immediately if asked to stop.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    logger.info("Quote monitoring worker stopped")
