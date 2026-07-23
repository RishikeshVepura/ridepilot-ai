"""In-memory per-session SSE event bus for the AI Service.

The AI Service is the single source of truth for what the frontend sees
(design rule 11). Server-pushed updates reach the browser over one SSE stream
per chat session: ``GET /api/stream/{user_id}/{chat_session_id}`` (Requirement
7.2 — each chat session has its own stream scoped by chat_session_id).

This module is the fan-out point between the producers of those events and the
connected SSE clients. It is a tiny in-process publish/subscribe broker keyed by
``(user_id, chat_session_id)``:

  - An SSE handler calls :meth:`EventBus.subscribe` to get its own
    ``asyncio.Queue`` and consumes events from it until the client disconnects,
    then calls :meth:`EventBus.unsubscribe` to clean up.
  - Producers call :meth:`EventBus.publish` to fan an event out to every queue
    currently subscribed for that session key.

Producers today are the chat message handler (task 6.2), which streams ``token``
and ``done`` events for the current turn; the same bus is reused by the event
consumer (task 6.4) to push ``quote_update``, ``ai_notification``,
``booking_update`` and ``ride_status`` events arriving on ``/internal/events``.

The implementation is deliberately in-memory and single-process — adequate for
the MVP (design section 11). The publish/subscribe surface is kept narrow so it
can later be swapped for Redis Pub/Sub or a similar broker without touching
callers.
"""

from __future__ import annotations

import asyncio
import uuid

# A session key uniquely identifies one chat session's stream. user_id is a
# plain string; chat_session_id is normalized to its string form so callers may
# pass either a uuid.UUID or a str without affecting routing.
SessionKey = tuple[str, str]

# Max events buffered per subscriber before back-pressure. A slow client should
# not be able to grow memory without bound; once full, the oldest event is
# dropped so the connection keeps moving (see EventBus.publish).
DEFAULT_QUEUE_MAXSIZE = 1000


def make_session_key(user_id: str, chat_session_id: uuid.UUID | str) -> SessionKey:
    """Build the canonical bus key for a chat session's stream.

    Args:
        user_id: The owning user.
        chat_session_id: The chat session id, as a UUID or its string form.

    Returns:
        A ``(user_id, chat_session_id_str)`` tuple usable as a dict key.
    """
    return (str(user_id), str(chat_session_id))


class EventBus:
    """An in-memory publish/subscribe broker scoped per chat session.

    Maintains, for each session key, the set of subscriber queues currently
    connected. Publishing an event copies it into every subscriber's queue so
    multiple clients (e.g. two browser tabs) on the same session each receive it.

    The bus is safe to use from many concurrent tasks: mutations of the
    subscriber registry are guarded by an asyncio.Lock, and the queues are
    themselves async-safe.
    """

    def __init__(self, queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> None:
        """Initialize an empty bus.

        Args:
            queue_maxsize: Per-subscriber buffer size before back-pressure
                handling (oldest-event drop) kicks in.
        """
        self._subscribers: dict[SessionKey, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        self._queue_maxsize = queue_maxsize

    async def subscribe(self, key: SessionKey) -> asyncio.Queue:
        """Register a new subscriber for a session key and return its queue.

        Each call creates a dedicated queue so concurrent clients on the same
        session are independent. The caller must pass the returned queue back to
        :meth:`unsubscribe` when it disconnects.

        Args:
            key: The session key to subscribe to (see :func:`make_session_key`).

        Returns:
            A fresh ``asyncio.Queue`` that will receive published events.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_maxsize)
        async with self._lock:
            self._subscribers.setdefault(key, set()).add(queue)
        return queue

    async def unsubscribe(self, key: SessionKey, queue: asyncio.Queue) -> None:
        """Remove a subscriber's queue and drop the key when it has none left.

        Safe to call more than once and with a queue that is already gone; this
        keeps disconnect cleanup simple for callers.

        Args:
            key: The session key the queue was subscribed to.
            queue: The queue previously returned by :meth:`subscribe`.
        """
        async with self._lock:
            subscribers = self._subscribers.get(key)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                # No listeners left for this session — forget the key entirely so
                # the registry does not accumulate empty entries over time.
                del self._subscribers[key]

    async def publish(self, key: SessionKey, event: dict) -> int:
        """Fan an event out to every subscriber currently on a session key.

        Each subscriber receives its own reference to ``event``. If a subscriber's
        queue is full (a slow or stalled client), its oldest buffered event is
        dropped to make room so a single slow consumer cannot block producers or
        grow memory without bound.

        Publishing to a key with no subscribers is a no-op and returns 0 — for
        example when the POST handler streams a turn before the frontend has
        opened the GET stream.

        Args:
            key: The session key to publish to.
            event: The event payload (a JSON-serializable dict, e.g.
                ``{"type": "token", "content": "Lyft "}``).

        Returns:
            The number of subscribers the event was delivered to.
        """
        async with self._lock:
            # Snapshot the subscriber set under the lock, then release it before
            # touching the queues so publishing never blocks subscribe/unsubscribe.
            subscribers = list(self._subscribers.get(key, ()))

        for queue in subscribers:
            self._offer(queue, event)
        return len(subscribers)

    def _offer(self, queue: asyncio.Queue, event: dict) -> None:
        """Put an event on a queue, dropping the oldest if it is full.

        Uses the non-blocking queue operations so a full (slow-client) queue
        applies back-pressure by discarding its stalest event rather than
        blocking the publishing producer.

        Args:
            queue: The subscriber queue to deliver to.
            event: The event payload to enqueue.
        """
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest event to make room for the newest, then retry once.
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Extremely unlikely (we just freed a slot); skip rather than block.
                pass

    async def subscriber_count(self, key: SessionKey) -> int:
        """Return how many subscribers are currently connected for a key.

        Primarily useful for diagnostics and tests.

        Args:
            key: The session key to inspect.

        Returns:
            The current subscriber count (0 if the key is unknown).
        """
        async with self._lock:
            return len(self._subscribers.get(key, ()))


# Process-wide singleton. All AI Service producers and SSE handlers share this
# one bus instance so events published by the chat handler (and, later, the
# /internal/events consumer in task 6.4) reach the right stream.
event_bus = EventBus()
