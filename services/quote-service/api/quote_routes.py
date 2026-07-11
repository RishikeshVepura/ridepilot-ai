"""Quote session endpoints for the Quote Service.

Thin HTTP layer over :class:`QuoteService`. Each handler validates the request
shape (via Pydantic), delegates to the service, and translates domain exceptions
(core.exceptions) into HTTP status codes. It contains no business logic or
database access.

    POST /quotes/sessions              create a session
    GET  /quotes/sessions/{id}         read a session's state + stored quotes
    POST /quotes/sessions/{id}/fetch   fetch + store quotes from all providers
    POST /quotes/sessions/{id}/select  record a selection
    POST /quotes/sessions/{id}/cancel  cancel a session

Requirements: 1.4, 2.1, 2.2, 4.1 (implemented in the service layer).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_quote_service
from core.exceptions import (
    MissingCoordinatesError,
    QuoteError,
    QuoteNotFoundError,
    QuoteNotInSessionError,
    QuoteSessionNotFoundError,
    SessionCancelledError,
)
from schemas.quote_schemas import (
    CreateSessionRequest,
    FetchQuotesResponse,
    QuoteSessionOut,
    SelectQuoteRequest,
    SelectQuoteResponse,
    SessionStateResponse,
)
from services.quote_service import QuoteService

router = APIRouter(prefix="/quotes", tags=["quotes"])

# Maps each domain exception to the HTTP status code the API should return.
_STATUS_BY_EXCEPTION: list[tuple[type[QuoteError], int]] = [
    (QuoteSessionNotFoundError, 404),
    (QuoteNotFoundError, 404),
    (SessionCancelledError, 409),
    (MissingCoordinatesError, 400),
    (QuoteNotInSessionError, 400),
]


def _to_http_exception(exc: QuoteError) -> HTTPException:
    """Translate a domain exception into the matching HTTPException.

    Args:
        exc: The raised domain error.

    Returns:
        An HTTPException with the mapped status code (500 as a safe default).
    """
    for exc_type, status_code in _STATUS_BY_EXCEPTION:
        if isinstance(exc, exc_type):
            return HTTPException(status_code=status_code, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post("/sessions", response_model=QuoteSessionOut, status_code=201)
async def create_session(
    req: CreateSessionRequest,
    service: QuoteService = Depends(get_quote_service),
) -> QuoteSessionOut:
    """Create a new quote session (Requirement 1.4)."""
    try:
        session = await service.create_session(req)
    except QuoteError as exc:
        raise _to_http_exception(exc) from exc
    return QuoteSessionOut.model_validate(session)


@router.get("/sessions/{session_id}", response_model=SessionStateResponse)
async def get_session_state(
    session_id: uuid.UUID,
    service: QuoteService = Depends(get_quote_service),
) -> SessionStateResponse:
    """Return a read-only snapshot of a session's current state and quotes."""
    try:
        return await service.get_session_state(session_id)
    except QuoteError as exc:
        raise _to_http_exception(exc) from exc


@router.post("/sessions/{session_id}/fetch", response_model=FetchQuotesResponse)
async def fetch_quotes(
    session_id: uuid.UUID,
    service: QuoteService = Depends(get_quote_service),
) -> FetchQuotesResponse:
    """Fetch quotes from all providers in parallel and store them (2.1, 2.2)."""
    try:
        return await service.fetch_quotes(session_id)
    except QuoteError as exc:
        raise _to_http_exception(exc) from exc


@router.post("/sessions/{session_id}/select", response_model=SelectQuoteResponse)
async def select_quote(
    session_id: uuid.UUID,
    req: SelectQuoteRequest,
    service: QuoteService = Depends(get_quote_service),
) -> SelectQuoteResponse:
    """Record the user's selection of a specific quote (Requirement 4.1)."""
    try:
        return await service.select_quote(session_id, req)
    except QuoteError as exc:
        raise _to_http_exception(exc) from exc


@router.post("/sessions/{session_id}/cancel", response_model=QuoteSessionOut)
async def cancel_session(
    session_id: uuid.UUID,
    service: QuoteService = Depends(get_quote_service),
) -> QuoteSessionOut:
    """Cancel a quote session."""
    try:
        session = await service.cancel_session(session_id)
    except QuoteError as exc:
        raise _to_http_exception(exc) from exc
    return QuoteSessionOut.model_validate(session)
