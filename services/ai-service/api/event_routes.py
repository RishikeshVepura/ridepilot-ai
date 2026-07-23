"""Internal background-event endpoint for the AI Service.

Thin HTTP layer over :class:`EventService`. Accepts the untrusted event envelope
posted by the Quote/Booking services at ``POST /internal/events``, validates it
against the matching Pydantic schema, and delegates routing to the service. It
contains no business logic.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_event_service, get_session
from schemas.event_schemas import BookingEventEnvelope, QuoteDeltaEvent
from services.event_service import EventService
from services.notifications import BOOKING_EVENT_TYPES, QUOTE_DELTA_EVENT_TYPE

logger = logging.getLogger("ai-service.events")

router = APIRouter(tags=["internal-events"])


@router.post("/internal/events")
async def receive_internal_event(
    request: Request,
    db: AsyncSession = Depends(get_session),
    event_service: EventService = Depends(get_event_service),
) -> dict:
    """Receive a background event from the Quote/Booking services and route it.

    Dispatches on ``event_type``: QUOTE_DELTA events and recognized booking events
    are validated against the matching Pydantic envelope and handed to the event
    service. The body is untrusted; a malformed body for a known type is rejected
    with 422. Unknown/unsupported event types are accepted but ignored so a future
    producer cannot break this endpoint.

    Returns:
        ``{"status": "accepted"}`` when an event was handled, or
        ``{"status": "ignored", ...}`` for an unsupported/unroutable event.

    Raises:
        HTTPException: 400 if the body is not a valid JSON object, 422 if a known
            event type fails validation.
    """
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - normalize any parse failure
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422, detail="event body must be a JSON object"
        )

    event_type = payload.get("event_type")

    if event_type == QUOTE_DELTA_EVENT_TYPE:
        try:
            quote_event = QuoteDeltaEvent.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        await event_service.handle_quote_delta(quote_event, db)
        return {"status": "accepted"}

    if event_type in BOOKING_EVENT_TYPES:
        try:
            booking_event = BookingEventEnvelope.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        await event_service.handle_booking_event(booking_event, db)
        return {"status": "accepted"}

    # Unknown or unsupported event type — accept without acting so producers are
    # never blocked by an event this consumer does not (yet) understand.
    logger.info("Ignoring unsupported internal event_type=%r", event_type)
    return {"status": "ignored", "reason": "unsupported event_type"}
