"""FastAPI dependency providers for the AI Service.

Wires the layers together: a per-request database session, plus the process-wide
service singletons (they are stateless aside from the shared stream publisher /
event bus, so one instance each is reused across requests).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from db.database import database
from infra.stream_publisher import stream_publisher
from services.event_service import EventService
from services.llm_service import LLMService
from services.responder import ChatResponder

# Service singletons. All share the one stream publisher (and thus the one event
# bus) so events they push reach the connected SSE streams.
_llm_service = LLMService(stream_publisher)
_chat_responder = ChatResponder(_llm_service, stream_publisher)
_event_service = EventService(stream_publisher)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a database session scoped to the request.

    Yields:
        An AsyncSession that is closed when the request finishes.
    """
    async with database.session_factory() as session:
        yield session


def get_chat_responder() -> ChatResponder:
    """Return the shared chat responder (live LLM + stub seam)."""
    return _chat_responder


def get_event_service() -> EventService:
    """Return the shared internal-event service."""
    return _event_service
