"""Typed publisher for server-pushed SSE events.

A thin, typed wrapper over the per-session :class:`EventBus`. Every server-pushed
event the frontend can receive on its ``GET /api/stream/...`` connection is
produced through one of these methods, so the exact event shapes live in one
place instead of being scattered as ad-hoc ``event_bus.publish({...})`` calls
across the LLM loop, the stub responder, and the internal-event consumer.

Each method resolves the ``(user_id, chat_session_id)`` stream key and fans the
event out to every subscriber on that session. Publishing to a session with no
subscribers is a harmless no-op (see :meth:`EventBus.publish`).
"""

from __future__ import annotations

import uuid

from infra.event_bus import EventBus, event_bus, make_session_key


class StreamPublisher:
    """Publishes the AI Service's server-pushed events to per-session streams."""

    def __init__(self, bus: EventBus) -> None:
        """Wire the publisher to the SSE event bus.

        Args:
            bus: The process-wide event bus to publish through.
        """
        self._bus = bus

    async def push_quote_snapshot(
        self, user_id: str, chat_session_id: uuid.UUID | None, quotes: list
    ) -> None:
        """Push freshly fetched quotes to a session stream as ``quote_snapshot``.

        Called right after ``fetch_quotes`` succeeds so the ride panel appears
        immediately. A None chat_session_id or empty quotes is a no-op.
        """
        if chat_session_id is None or not quotes:
            return
        key = make_session_key(user_id, chat_session_id)
        await self._bus.publish(key, {"type": "quote_snapshot", "quotes": quotes})

    async def push_route_map(
        self, user_id: str, chat_session_id: uuid.UUID | None, session: dict | None
    ) -> None:
        """Push pickup/dropoff coordinates to a session stream as ``route_map``.

        Called right after ``create_quote_session`` succeeds so the frontend can
        render a map with both points as soon as the search starts. A None
        chat_session_id, missing session payload, or missing coordinates is a
        no-op (nothing to plot).

        Args:
            user_id: The owning user (stream routing key).
            chat_session_id: The chat session whose stream to push to.
            session: The created QuoteSessionOut dict (coords + address labels).
        """
        if chat_session_id is None or not isinstance(session, dict):
            return

        pickup_lat = session.get("pickup_lat")
        pickup_lng = session.get("pickup_lng")
        dropoff_lat = session.get("dropoff_lat")
        dropoff_lng = session.get("dropoff_lng")
        if None in (pickup_lat, pickup_lng, dropoff_lat, dropoff_lng):
            return

        key = make_session_key(user_id, chat_session_id)
        await self._bus.publish(
            key,
            {
                "type": "route_map",
                "pickup": {
                    "lat": float(pickup_lat),
                    "lng": float(pickup_lng),
                    "label": session.get("pickup_address") or "Pickup",
                },
                "dropoff": {
                    "lat": float(dropoff_lat),
                    "lng": float(dropoff_lng),
                    "label": session.get("dropoff_address") or "Dropoff",
                },
            },
        )

    async def push_booking_created(
        self,
        user_id: str,
        chat_session_id: uuid.UUID | None,
        booking_id: uuid.UUID | str,
    ) -> None:
        """Push a ``booking_created`` event so the UI hides the ride cards.

        A None chat_session_id or booking_id is a no-op.
        """
        if chat_session_id is None or not booking_id:
            return
        key = make_session_key(user_id, chat_session_id)
        await self._bus.publish(
            key, {"type": "booking_created", "booking_id": str(booking_id)}
        )

    async def push_quote_update(
        self, user_id: str, chat_session_id: uuid.UUID, changes: list[dict]
    ) -> None:
        """Push a silent ``quote_update`` (card refresh) for a QUOTE_DELTA."""
        key = make_session_key(user_id, chat_session_id)
        await self._bus.publish(key, {"type": "quote_update", "changes": changes})

    async def push_ai_notification(
        self, user_id: str, chat_session_id: uuid.UUID, message: str
    ) -> None:
        """Push an interrupting ``ai_notification`` message to the session."""
        key = make_session_key(user_id, chat_session_id)
        await self._bus.publish(
            key, {"type": "ai_notification", "message": message}
        )

    async def push_booking_update(
        self,
        user_id: str,
        chat_session_id: uuid.UUID,
        booking_id: str,
        status: str | None,
    ) -> None:
        """Push a ``booking_update`` carrying the booking's current status."""
        key = make_session_key(user_id, chat_session_id)
        await self._bus.publish(
            key,
            {"type": "booking_update", "booking_id": booking_id, "status": status},
        )

    async def push_ride_status(
        self,
        user_id: str,
        chat_session_id: uuid.UUID,
        booking_id: str,
        message: str,
    ) -> None:
        """Push a spoken ``ride_status`` milestone message to the session."""
        key = make_session_key(user_id, chat_session_id)
        await self._bus.publish(
            key,
            {"type": "ride_status", "booking_id": booking_id, "message": message},
        )


# Process-wide singleton sharing the same event bus every SSE handler subscribes
# to, so events published here reach the connected streams.
stream_publisher = StreamPublisher(event_bus)
