"""Backend tool layer for the AI Service — the ONLY way the assistant acts.

This module is the heart of the tool-calling boundary the design and Requirement
9 mandate. The LLM (or, with no API key, the stub intent simulator) never touches
the database, never calls a mock provider, and never confirms a booking on its
own. Instead it may only *request a named tool* defined here; each tool validates
nothing about provider/DB state itself but delegates to the authoritative Quote
and Booking services over HTTP, which own all quote/booking/ride state.

Boundary enforcement (Requirements 9.1–9.5):
  9.1 The AI SHALL NOT write to the database directly — these tools make no DB
      writes; the only persistence the AI Service performs is its own chat state
      (handled in repository.py), never quote/booking/ride state.
  9.2 The AI SHALL NOT call mock provider APIs directly — tools call the Quote
      and Booking services only; those services are the sole callers of the mock
      providers. There is deliberately no mock-providers client here.
  9.3 The AI SHALL NOT confirm a booking directly — confirmation is a Booking
      Service operation (:func:`confirm_booking`) that forwards an explicit
      ``confirmed`` flag; the service rejects anything but an explicit true
      (Requirement 5.6). The AI cannot bypass that check.
  9.4 WHEN the AI needs to act it SHALL request a named backend tool and the
      backend SHALL validate state and execute — every action below maps to a
      named tool the LLM selects by name via the schemas in :data:`TOOL_SCHEMAS`
      and the dispatcher in :data:`TOOL_DISPATCH`.
  9.5 The backend SHALL remain the source of truth — tools return whatever the
      Quote/Booking services report; they never invent or cache authoritative
      state.

Shape of a tool result: every tool returns a plain ``dict`` so it can be fed back
into the LLM tool-call loop (serialized to JSON) or consumed by the stub path.
On success the dict carries the upstream payload; on failure it carries a clean
``{"success": False, "error": ...}`` structure instead of raising into the loop,
so an upstream hiccup becomes an assistant-visible message rather than a 500 in
the SSE stream.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from obs import truncate

logger = logging.getLogger("ai-service.tools")


@dataclass(frozen=True)
class ToolContext:
    """Trusted identity + ride state carried into the tool-calling seam.

    Built in routes.py from the loaded :class:`ConversationContext` and passed to
    both the live LLM loop and the stub path. ``user_id`` and ``chat_session_id``
    are authoritative (server-derived, never model-supplied) and are injected
    into identity-bearing tools so the model cannot act as another user
    (Requirement 9.4). The optional location is the GPS pickup the frontend sent
    with this turn (Requirement 1.3), available to seed a quote session.

    The quote/booking ids reflect the chat session's currently linked ride state
    so the stub path can resume mid-flow across turns; they are advisory hints,
    with the Quote/Booking services remaining the source of truth (Requirement
    9.5).
    """

    user_id: str
    chat_session_id: uuid.UUID
    quote_session_id: uuid.UUID | None = None
    booking_id: uuid.UUID | None = None
    pickup_lat: float | None = None
    pickup_lng: float | None = None

# Inter-service base URLs. Defaults target the docker-compose network (service
# name + internal port) so the AI Service container reaches its peers directly;
# both are overridable via the documented env vars for other environments.
QUOTE_SERVICE_URL_ENV = "QUOTE_SERVICE_URL"
BOOKING_SERVICE_URL_ENV = "BOOKING_SERVICE_URL"
DEFAULT_QUOTE_SERVICE_URL = "http://quote-service:8002"
DEFAULT_BOOKING_SERVICE_URL = "http://booking-service:8003"

# Timeout (seconds) for any single upstream call. Quote fetching fans out to all
# providers server-side, so allow a little headroom while still bounding hangs.
UPSTREAM_TIMEOUT_SECONDS = 30.0

# Fixed test coordinates. The system has no address geocoding yet, so it does not
# know the real lat/lng for a pickup or dropoff — and the LLM must NOT invent them
# (it has no way to know real coordinates and would fabricate values). Until a
# proper geocoding step exists, these stand-in coordinates (downtown Phoenix →
# Sky Harbor airport by default) are injected whenever a coordinate is missing, so
# the Quote/Booking services — which require all four — can still operate. The
# mock providers ignore coordinates for pricing, so the values only need to be
# valid. Override via env to test a different locale.
TEST_PICKUP_LAT = float(os.getenv("TEST_PICKUP_LAT", "33.42478425099026"))
TEST_PICKUP_LNG = float(os.getenv("TEST_PICKUP_LNG", "-111.94232584659265"))
TEST_DROPOFF_LAT = float(os.getenv("TEST_DROPOFF_LAT", "33.43561388126599"))
TEST_DROPOFF_LNG = float(os.getenv("TEST_DROPOFF_LNG", "-112.010214086741"))


def _quote_base_url() -> str:
    """Resolve the Quote Service base URL from the environment.

    Returns:
        The configured QUOTE_SERVICE_URL, or the docker-network default. Any
        trailing slash is stripped so path joins stay clean.
    """
    return os.getenv(QUOTE_SERVICE_URL_ENV, DEFAULT_QUOTE_SERVICE_URL).rstrip("/")


def _booking_base_url() -> str:
    """Resolve the Booking Service base URL from the environment.

    Returns:
        The configured BOOKING_SERVICE_URL, or the docker-network default. Any
        trailing slash is stripped so path joins stay clean.
    """
    return os.getenv(BOOKING_SERVICE_URL_ENV, DEFAULT_BOOKING_SERVICE_URL).rstrip("/")


class ToolError(Exception):
    """Raised internally when an upstream service call fails.

    Carries the originating tool name plus a human-readable message (and, when
    available, the upstream HTTP status). Tool functions catch this and convert
    it into a structured error dict, so it never escapes into the LLM loop or the
    SSE stream as an unhandled exception.
    """

    def __init__(
        self, tool: str, message: str, *, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.tool = tool
        self.message = message
        self.status_code = status_code


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    """Wrap a successful upstream payload in the standard tool-result envelope.

    Args:
        data: The upstream response body (already JSON-decoded).

    Returns:
        ``{"success": True, "data": data}``.
    """
    return {"success": True, "data": data}


def _err(tool: str, message: str, status_code: int | None = None) -> dict[str, Any]:
    """Build the standard error envelope for a failed tool call.

    Args:
        tool: The tool that failed (for logging/telemetry and LLM context).
        message: A clean, assistant-safe description of what went wrong.
        status_code: The upstream HTTP status, when the failure was an HTTP error.

    Returns:
        ``{"success": False, "tool": ..., "error": ..., "status_code": ...}``.
    """
    return {
        "success": False,
        "tool": tool,
        "error": message,
        "status_code": status_code,
    }


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``payload`` without keys whose value is ``None``.

    Optional pickup/dropoff fields are passed through as keyword arguments; this
    keeps them out of the JSON body when unset so upstream defaults apply rather
    than sending explicit nulls.

    Args:
        payload: The candidate request body.

    Returns:
        A new dict containing only the keys with non-None values.
    """
    return {key: value for key, value in payload.items() if value is not None}


def _stringify_ids(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce any UUID values in a payload to strings for JSON serialization.

    Args:
        payload: A request body that may contain ``uuid.UUID`` values.

    Returns:
        A new dict with UUIDs replaced by their string form; other values are
        passed through unchanged.
    """
    return {
        key: (str(value) if isinstance(value, uuid.UUID) else value)
        for key, value in payload.items()
    }


async def _request(
    tool: str,
    method: str,
    base_url: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make one HTTP call to an upstream service and return its JSON body.

    Centralizes client construction, timeout handling, and error normalization so
    every tool behaves identically: any transport error or non-2xx response is
    converted into a :class:`ToolError` with a clean message (including the
    upstream ``detail`` when present), never a raw exception leaking upstream
    internals into the assistant's reply.

    Args:
        tool: The calling tool's name, for error attribution.
        method: HTTP method ("GET" or "POST").
        base_url: The upstream service base URL.
        path: The request path (joined to ``base_url``).
        json_body: Optional JSON request body for POSTs.

    Returns:
        The decoded JSON response body as a dict.

    Raises:
        ToolError: On connection failure, timeout, or a non-2xx status.
    """
    url = f"{base_url}{path}"
    if json_body is not None:
        logger.info("→ %s  %s %s  body=%s", tool, method, url, truncate(json_body))
    else:
        logger.info("→ %s  %s %s", tool, method, url)

    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
            response = await client.request(method, url, json=json_body)
    except httpx.RequestError as exc:
        # DNS/connection/timeout — the service is unreachable or too slow.
        logger.warning("✗ %s  %s  unreachable: %s", tool, url, exc)
        raise ToolError(
            tool, f"could not reach the {tool} backend service: {exc}"
        ) from exc

    if response.status_code >= 400:
        # Try to surface the upstream's structured detail; fall back to text.
        detail: Any
        try:
            body = response.json()
            detail = body.get("detail", body) if isinstance(body, dict) else body
        except ValueError:
            detail = response.text
        logger.warning(
            "✗ %s  HTTP %s  detail=%s",
            tool,
            response.status_code,
            truncate(detail),
        )
        raise ToolError(
            tool,
            f"the backend rejected the request (HTTP {response.status_code}): {detail}",
            status_code=response.status_code,
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise ToolError(
            tool, "the backend returned a response that could not be parsed"
        ) from exc

    logger.info("← %s  HTTP %s  body=%s", tool, response.status_code, truncate(data))
    return data


# ---------------------------------------------------------------------------
# Quote Service tools
# ---------------------------------------------------------------------------


async def create_quote_session(
    user_id: str,
    chat_session_id: uuid.UUID | str | None = None,
    pickup_address: str | None = None,
    pickup_lat: float | None = None,
    pickup_lng: float | None = None,
    dropoff_address: str | None = None,
    dropoff_lat: float | None = None,
    dropoff_lng: float | None = None,
) -> dict[str, Any]:
    """Create a quote session in the Quote Service (Requirement 1.4).

    Pickup/dropoff are optional at creation; coordinates are required upstream
    before quotes can be fetched, so a session may be created with addresses only
    and refined later.

    Args:
        user_id: The owning user.
        chat_session_id: The chat session this search belongs to.
        pickup_address: Optional free-form pickup label.
        pickup_lat: Optional pickup latitude.
        pickup_lng: Optional pickup longitude.
        dropoff_address: Optional free-form dropoff label.
        dropoff_lat: Optional dropoff latitude.
        dropoff_lng: Optional dropoff longitude.

    Returns:
        A tool-result envelope wrapping the created QuoteSessionOut on success.
    """
    # Backfill any missing coordinates with the fixed test coordinates. A real
    # GPS pickup (when the frontend sends it) still takes precedence; this only
    # fills what is unknown so the model never needs to supply — or fabricate —
    # lat/lng. Remove once real geocoding is in place.
    if pickup_lat is None or pickup_lng is None:
        pickup_lat = TEST_PICKUP_LAT
        pickup_lng = TEST_PICKUP_LNG
    if dropoff_lat is None or dropoff_lng is None:
        dropoff_lat = TEST_DROPOFF_LAT
        dropoff_lng = TEST_DROPOFF_LNG

    body = _stringify_ids(
        _drop_none(
            {
                "user_id": user_id,
                "chat_session_id": chat_session_id,
                "pickup_address": pickup_address,
                "pickup_lat": pickup_lat,
                "pickup_lng": pickup_lng,
                "dropoff_address": dropoff_address,
                "dropoff_lat": dropoff_lat,
                "dropoff_lng": dropoff_lng,
            }
        )
    )
    try:
        data = await _request(
            "create_quote_session",
            "POST",
            _quote_base_url(),
            "/quotes/sessions",
            json_body=body,
        )
    except ToolError as exc:
        return _err(exc.tool, exc.message, exc.status_code)
    return _ok(data)


async def get_quote_session(quote_session_id: uuid.UUID | str) -> dict[str, Any]:
    """Read a quote session's current state and stored quotes (read-only).

    Internal helper (not exposed to the LLM as a tool): used to build the
    authoritative ride-state summary injected into each turn so the model can see
    what is already known — pickup/dropoff, search status, and whether quotes
    have been fetched — instead of re-asking. Never mutates state.

    Args:
        quote_session_id: The quote session to read.

    Returns:
        A tool-result envelope wrapping the SessionStateResponse on success.
    """
    try:
        data = await _request(
            "get_quote_session",
            "GET",
            _quote_base_url(),
            f"/quotes/sessions/{quote_session_id}",
        )
    except ToolError as exc:
        return _err(exc.tool, exc.message, exc.status_code)
    return _ok(data)


async def fetch_quotes(quote_session_id: uuid.UUID | str) -> dict[str, Any]:
    """Fetch quotes from all providers for a quote session (Requirements 2.1, 2.2).

    The Quote Service fans out to every provider in parallel and normalizes the
    results; this tool simply returns that structured payload (quotes plus any
    unavailable providers) for the assistant to summarize (Requirement 2.4).

    Args:
        quote_session_id: The quote session to fetch quotes for.

    Returns:
        A tool-result envelope wrapping the FetchQuotesResponse on success.
    """
    try:
        data = await _request(
            "fetch_quotes",
            "POST",
            _quote_base_url(),
            f"/quotes/sessions/{quote_session_id}/fetch",
        )
    except ToolError as exc:
        return _err(exc.tool, exc.message, exc.status_code)
    return _ok(data)


async def select_quote(
    quote_session_id: uuid.UUID | str,
    provider: str,
    ride_type: str,
) -> dict[str, Any]:
    """Record the user's chosen quote by provider and ride type (Requirement 4.1).

    The model tells us which option the user picked (e.g. provider="uber",
    ride_type="UberX"). This function resolves the actual quote_id by reading
    the session's current quotes, then calls the Quote Service /select endpoint.
    The model never needs to know or supply a quote_id.

    Args:
        quote_session_id: The quote session the selection belongs to.
        provider: The chosen provider key (e.g. "uber", "lyft", "waymo").
        ride_type: The chosen ride type label (e.g. "UberX", "Lyft Standard").

    Returns:
        A tool-result envelope wrapping the SelectQuoteResponse on success, or
        an error envelope when the provider/ride_type combination is not found.
    """
    # Fetch the current quotes for this session so we can resolve the quote_id.
    try:
        state = await _request(
            "select_quote",
            "GET",
            _quote_base_url(),
            f"/quotes/sessions/{quote_session_id}",
        )
    except ToolError as exc:
        return _err(exc.tool, exc.message, exc.status_code)

    quotes = state.get("quotes") or []
    # Match case-insensitively so "uber" matches "Uber", "uberx" matches "UberX", etc.
    provider_lower = provider.lower().strip()
    ride_type_lower = ride_type.lower().strip()
    matched = next(
        (
            q for q in quotes
            if q.get("provider", "").lower() == provider_lower
            and q.get("ride_type", "").lower() == ride_type_lower
        ),
        None,
    )
    if matched is None:
        # Try a partial ride_type match (e.g. "uberx" matches "UberX", "comfort" matches "Uber Comfort")
        matched = next(
            (
                q for q in quotes
                if q.get("provider", "").lower() == provider_lower
                and ride_type_lower in q.get("ride_type", "").lower()
            ),
            None,
        )
    if matched is None:
        # Last resort: match ride_type across any provider
        matched = next(
            (
                q for q in quotes
                if ride_type_lower in q.get("ride_type", "").lower()
            ),
            None,
        )
    if matched is None:
        return _err(
            "select_quote",
            f"Could not find a quote for {provider} {ride_type} in the current session. "
            "Available options: " + ", ".join(
                f"{q.get('provider')} {q.get('ride_type')}" for q in quotes
            ),
        )

    quote_id = matched["id"]
    body = _stringify_ids({"quote_id": quote_id})
    try:
        data = await _request(
            "select_quote",
            "POST",
            _quote_base_url(),
            f"/quotes/sessions/{quote_session_id}/select",
            json_body=body,
        )
    except ToolError as exc:
        return _err(exc.tool, exc.message, exc.status_code)
    return _ok(data)


async def cancel_quote_session(quote_session_id: uuid.UUID | str) -> dict[str, Any]:
    """Cancel a quote session, stopping any monitoring.

    Args:
        quote_session_id: The quote session to cancel.

    Returns:
        A tool-result envelope wrapping the updated QuoteSessionOut on success.
    """
    try:
        data = await _request(
            "cancel_quote_session",
            "POST",
            _quote_base_url(),
            f"/quotes/sessions/{quote_session_id}/cancel",
        )
    except ToolError as exc:
        return _err(exc.tool, exc.message, exc.status_code)
    return _ok(data)


# ---------------------------------------------------------------------------
# Booking Service tools
# ---------------------------------------------------------------------------


async def create_booking(
    user_id: str,
    chat_session_id: uuid.UUID | str | None,
    provider: str,
    ride_type: str,
    selected_price: float,
    quote_session_id: uuid.UUID | str | None = None,
    quote_id: uuid.UUID | str | None = None,
    pickup_address: str | None = None,
    pickup_lat: float | None = None,
    pickup_lng: float | None = None,
    dropoff_address: str | None = None,
    dropoff_lat: float | None = None,
    dropoff_lng: float | None = None,
    currency: str | None = None,
    pickup_eta_minutes: int | None = None,
) -> dict[str, Any]:
    """Create a booking session in the Booking Service (Requirement 5.1).

    This does NOT confirm anything — it creates a booking in its initial state so
    the price can subsequently be re-verified (:func:`verify_booking`) and, only
    after explicit user approval, confirmed (:func:`confirm_booking`).

    Args:
        user_id: The owning user.
        chat_session_id: The chat session this booking belongs to.
        provider: The chosen provider (e.g. "Uber").
        ride_type: The chosen ride type (e.g. "UberX").
        selected_price: The price the user selected, for later comparison.
        quote_session_id: Optional originating quote session.
        quote_id: Optional originating quote.
        pickup_address: Optional pickup label.
        pickup_lat: Optional pickup latitude.
        pickup_lng: Optional pickup longitude.
        dropoff_address: Optional dropoff label.
        dropoff_lat: Optional dropoff latitude.
        dropoff_lng: Optional dropoff longitude.
        currency: Optional ISO currency code (defaults to USD upstream).
        pickup_eta_minutes: Optional pickup ETA in minutes.

    Returns:
        A tool-result envelope wrapping the created BookingOut on success.
    """
    # Backfill any missing coordinates with the fixed test coordinates, matching
    # create_quote_session, so the model never supplies lat/lng. Remove once real
    # geocoding is in place.
    if pickup_lat is None or pickup_lng is None:
        pickup_lat = TEST_PICKUP_LAT
        pickup_lng = TEST_PICKUP_LNG
    if dropoff_lat is None or dropoff_lng is None:
        dropoff_lat = TEST_DROPOFF_LAT
        dropoff_lng = TEST_DROPOFF_LNG

    body = _stringify_ids(
        _drop_none(
            {
                "user_id": user_id,
                "chat_session_id": chat_session_id,
                "provider": provider,
                "ride_type": ride_type,
                "selected_price": selected_price,
                "quote_session_id": quote_session_id,
                "quote_id": quote_id,
                "pickup_address": pickup_address,
                "pickup_lat": pickup_lat,
                "pickup_lng": pickup_lng,
                "dropoff_address": dropoff_address,
                "dropoff_lat": dropoff_lat,
                "dropoff_lng": dropoff_lng,
                "currency": currency,
                "pickup_eta_minutes": pickup_eta_minutes,
            }
        )
    )
    try:
        data = await _request(
            "create_booking",
            "POST",
            _booking_base_url(),
            "/bookings",
            json_body=body,
        )
    except ToolError as exc:
        return _err(exc.tool, exc.message, exc.status_code)
    return _ok(data)


async def verify_booking(booking_id: uuid.UUID | str) -> dict[str, Any]:
    """Re-verify a booking's final price with the provider (Requirements 5.2, 5.3).

    Returns the selected vs final price and whether they differ so the assistant
    can decide whether explicit re-confirmation is required before confirming.

    Args:
        booking_id: The booking to re-verify.

    Returns:
        A tool-result envelope wrapping the VerifyBookingResponse on success.
    """
    try:
        data = await _request(
            "verify_booking",
            "POST",
            _booking_base_url(),
            f"/bookings/{booking_id}/verify",
        )
    except ToolError as exc:
        return _err(exc.tool, exc.message, exc.status_code)
    return _ok(data)


async def confirm_booking(
    booking_id: uuid.UUID | str, confirmed: bool
) -> dict[str, Any]:
    """Confirm a booking — ONLY with explicit user approval (Requirements 5.4, 5.6).

    The ``confirmed`` flag is forwarded as-is to the Booking Service, which is the
    authority on confirmation: it rejects anything but an explicit ``true`` (HTTP
    422). This is the boundary that prevents the LLM from confirming on its own
    (Requirement 9.3) — if the assistant tries to confirm without the user having
    said yes, the upstream rejection is surfaced rather than silently honored.

    Args:
        booking_id: The booking to confirm.
        confirmed: Must be ``True`` (explicit user approval). Any other value is
            forwarded and rejected upstream.

    Returns:
        A tool-result envelope wrapping the ConfirmBookingResponse on success, or
        the upstream rejection on failure.
    """
    body = {"confirmed": bool(confirmed)}
    try:
        data = await _request(
            "confirm_booking",
            "POST",
            _booking_base_url(),
            f"/bookings/{booking_id}/confirm",
            json_body=body,
        )
    except ToolError as exc:
        return _err(exc.tool, exc.message, exc.status_code)
    return _ok(data)


async def cancel_booking(booking_id: uuid.UUID | str) -> dict[str, Any]:
    """Cancel a booking with the provider (Requirement 6.4).

    Args:
        booking_id: The booking to cancel.

    Returns:
        A tool-result envelope wrapping the updated BookingOut on success.
    """
    try:
        data = await _request(
            "cancel_booking",
            "POST",
            _booking_base_url(),
            f"/bookings/{booking_id}/cancel",
        )
    except ToolError as exc:
        return _err(exc.tool, exc.message, exc.status_code)
    return _ok(data)


async def get_booking_events(booking_id: uuid.UUID | str) -> dict[str, Any]:
    """Fetch the ride timeline (booking events) for a booking (Requirement 6.2).

    Args:
        booking_id: The booking whose events to fetch.

    Returns:
        A tool-result envelope wrapping the BookingEventsResponse on success.
    """
    try:
        data = await _request(
            "get_booking_events",
            "GET",
            _booking_base_url(),
            f"/bookings/{booking_id}/events",
        )
    except ToolError as exc:
        return _err(exc.tool, exc.message, exc.status_code)
    return _ok(data)


# ---------------------------------------------------------------------------
# Tool registry: OpenAI function schemas + name -> callable dispatch map.
#
# These two structures are kept together so the live LLM path (which advertises
# TOOL_SCHEMAS to the model and dispatches the model's chosen tool through
# TOOL_DISPATCH) and the stub path (which calls dispatch entries directly) share
# exactly one source of truth for what tools exist and what they accept.
#
# Note on identity parameters: user_id and chat_session_id are sourced from the
# trusted conversation context (the ToolContext built in routes.py), NOT from the
# model. They are therefore intentionally omitted from the schemas below so the
# model cannot spoof another user's identity; llm.py injects them when invoking
# create_quote_session / create_booking. (Requirement 9.4 — the backend, not the
# model, controls who the action runs as.)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_quote_session",
            "description": (
                "Start a new ride search for the user. Call this once both a "
                "pickup and dropoff are known (Requirement 1.4). Pickup may be "
                "the user's current GPS location or an entered address. Provide "
                "only the address labels; the backend resolves the actual "
                "coordinates — never pass or guess latitude/longitude."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pickup_address": {
                        "type": "string",
                        "description": "Free-form pickup label, if given.",
                    },
                    "dropoff_address": {
                        "type": "string",
                        "description": "Free-form dropoff label, if given.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_quotes",
            "description": (
                "Fetch ride quotes from all providers in parallel for the active "
                "quote session, then summarize the cheapest and fastest options "
                "to the user (Requirements 2.1, 2.2, 2.4). The backend targets the "
                "active session automatically — take no id parameters."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_quote",
            "description": (
                "Record the specific quote the user chose to move toward booking "
                "(Requirement 4.1). Pass the provider and ride_type using the EXACT "
                "values listed under 'Available options' in the ride state block — "
                "copy them verbatim, do not add a provider prefix. The backend "
                "resolves the actual quote and the active session — never pass or guess any id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "Exact provider value from the Available options list, e.g. 'uber', 'lyft', 'waymo'.",
                    },
                    "ride_type": {
                        "type": "string",
                        "description": "Exact ride_type value from the Available options list, e.g. 'UberX', 'Wait & Save'.",
                    },
                },
                "required": ["provider", "ride_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_quote_session",
            "description": (
                "Cancel the active ride search and stop monitoring its quotes. "
                "The backend targets the active session automatically — takes no id."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": (
                "Create a booking session for the selected ride (Requirement 5.1). "
                "This does NOT confirm the ride; always follow with verify_booking "
                "and ask the user for explicit approval before confirm_booking. "
                "The backend links it to the active quote session and resolves all "
                "ids and coordinates — pass only provider, ride_type, and the "
                "selected_price the user is booking at."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string"},
                    "ride_type": {"type": "string"},
                    "selected_price": {"type": "number"},
                    "currency": {"type": "string"},
                    "pickup_eta_minutes": {"type": "integer"},
                },
                "required": ["provider", "ride_type", "selected_price"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_booking",
            "description": (
                "Re-verify the final price of the active booking with the provider "
                "before confirmation (Requirements 5.2, 5.3). If the price changed, "
                "tell the user the new price and require explicit re-confirmation. "
                "The backend targets the active booking automatically — takes no id."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_booking",
            "description": (
                "Confirm the active booking with the provider. ONLY call this after "
                "the user has explicitly approved the final price in their latest "
                "message; pass confirmed=true. The backend targets the active "
                "booking automatically — pass only confirmed. Never confirm on your "
                "own (Requirements 5.4, 5.6, 9.3)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmed": {
                        "type": "boolean",
                        "description": (
                            "Must be true and only set when the user explicitly "
                            "approved this booking."
                        ),
                    },
                },
                "required": ["confirmed"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_booking",
            "description": (
                "Cancel the active booking with the provider (Requirement 6.4). "
                "The backend targets the active booking automatically — takes no id."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_booking_events",
            "description": (
                "Fetch the ride timeline (status milestones) for the active booking "
                "(Requirement 6.2). The backend targets the active booking "
                "automatically — takes no id."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

# Maps the tool names advertised above to the async callables that execute them.
# This is the single dispatch point used by both the live LLM loop and the stub.
TOOL_DISPATCH: dict[str, Any] = {
    "create_quote_session": create_quote_session,
    "fetch_quotes": fetch_quotes,
    "select_quote": select_quote,
    "cancel_quote_session": cancel_quote_session,
    "create_booking": create_booking,
    "verify_booking": verify_booking,
    "confirm_booking": confirm_booking,
    "cancel_booking": cancel_booking,
    "get_booking_events": get_booking_events,
}
