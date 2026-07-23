"""Assistant response generation for the AI Service chat turn.

:class:`ChatResponder` owns the "what does the assistant say" step of a chat turn
and the *streaming* of that reply token by token so the frontend can render and
speak it as it arrives (Requirements 1.1, 1.2, 1.3). It is the single seam the
API layer depends on: :meth:`ChatResponder.stream_reply` yields token strings and
the chat route turns each into a ``token`` SSE event.

Two backends sit behind that seam:

  - Live LLM path — when a live endpoint is configured, the call delegates to
    :meth:`LLMService.stream_reply`, which runs a streaming tool-calling loop
    where the model decides intent and requests named backend tools.

  - Stub path — when no usable key/endpoint is set, this class runs a lightweight
    keyword/intent simulator. Crucially the stub still drives the *real* backend
    tools in tools.tools, so the entire ride flow (create quote session → fetch
    quotes → select → create/verify/confirm/cancel booking) is demoable end to end
    without any API key, exercising the same boundaries (Requirements 9.1–9.5).

Both paths preserve the boundary rules: neither writes ride/booking state nor
talks to providers directly; all such actions go through the validated Quote and
Booking services via tools. The only persistence done here is the AI Service's
own chat state — linking the chat session to its active quote session / booking
so the stub can resume mid-flow across turns.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.obs import truncate
from infra.stream_publisher import StreamPublisher
from repositories.chat_repository import ChatRepository
from schemas.chat_schemas import ConversationContext
from services.llm_service import LLMService
from tools import tools
from tools.tools import ToolContext

logger = logging.getLogger("ai-service.responder")

# Optional artificial delay between streamed tokens, in seconds. Defaults to 0
# (stream as fast as possible). A small value can be set via env to make the
# streaming visible when manually exercising the endpoint.
TOKEN_DELAY_SECONDS_ENV = "STUB_TOKEN_DELAY_SECONDS"

# Affirmative phrases that count as explicit booking approval in the stub path
# (Requirement 5.6 — confirmation is never implicit; the user must say yes).
_AFFIRMATIVE_PHRASES = (
    "yes",
    "confirm",
    "go ahead",
    "do it",
    "book it",
    "sounds good",
    "let's do it",
    "lets do it",
    "that works",
    "ok book",
)

# Known provider names, used to map a user's mention ("book the Lyft") to a quote.
_KNOWN_PROVIDERS = ("uber", "lyft", "waymo")


def _token_delay_seconds() -> float:
    """Resolve the inter-token delay from the environment.

    Returns the configured delay in seconds, or 0.0 when unset, non-numeric, or
    negative.
    """
    raw = os.getenv(TOKEN_DELAY_SECONDS_ENV)
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def _tokenize(text: str) -> list[str]:
    """Split text into streamable chunks, preserving the trailing space.

    Each chunk is a word plus the single space that follows it (the last word has
    no trailing space), matching the design's token shape so the frontend can
    concatenate chunks directly without re-inserting spaces.
    """
    words = text.split()
    if not words:
        return []
    return [f"{word} " for word in words[:-1]] + [words[-1]]


def _mentions(text: str, phrases: tuple[str, ...]) -> bool:
    """Return whether any of ``phrases`` appears in the lowercased ``text``."""
    return any(phrase in text for phrase in phrases)


def _is_affirmative(text: str) -> bool:
    """Return whether the message reads as explicit approval to confirm."""
    return any(phrase in text for phrase in _AFFIRMATIVE_PHRASES)


def _is_ride_request(text: str) -> bool:
    """Heuristically detect a new ride-search request."""
    keywords = ("ride", "go to", "take me", "get me", "need a", "find me", "trip to")
    if _mentions(text, keywords):
        return True
    # A bare "to <place>" also reads as a destination.
    return text.startswith("to ") or " to " in text


def _extract_dropoff(message: str) -> str | None:
    """Pull a best-effort dropoff label out of a free-form ride request.

    Looks for the text following a "to"/"to the" marker, e.g. "find me a ride to
    the airport" -> "the airport". A deliberately simple heuristic for the no-key
    demo path; the live LLM extracts locations far more robustly.
    """
    match = re.search(r"\bto\s+(.+)$", message.strip(), flags=re.IGNORECASE)
    if not match:
        return None
    candidate = match.group(1).strip().rstrip(".!?")
    return candidate or None


def _format_price(price: Any, currency: str | None) -> str:
    """Format a price + currency for display, defaulting to a ``$`` prefix."""
    try:
        amount = f"{float(price):.2f}"
    except (TypeError, ValueError):
        amount = str(price)
    symbol = "$" if not currency or currency.upper() == "USD" else f"{currency} "
    return f"{symbol}{amount}"


def _summarize_quotes(quotes: list[dict[str, Any]]) -> str:
    """Compose the cheapest/fastest summary line for fetched quotes (Req 2.4)."""
    available = [q for q in quotes if q.get("available", True)]
    if not available:
        return "I couldn't find any available rides right now. Want me to try again in a moment?"

    cheapest = min(available, key=lambda q: q.get("price", float("inf")))

    def eta_key(q: dict[str, Any]) -> float:
        eta = q.get("pickup_eta_minutes")
        return float(eta) if eta is not None else float("inf")

    fastest = min(available, key=eta_key)

    cheapest_eta = cheapest.get("pickup_eta_minutes")
    cheapest_line = (
        f"{cheapest.get('provider')} {cheapest.get('ride_type')} is cheapest at "
        f"{_format_price(cheapest.get('price'), cheapest.get('currency'))}"
        + (f" with a {cheapest_eta}-minute pickup" if cheapest_eta is not None else "")
        + "."
    )

    if fastest.get("id") == cheapest.get("id"):
        return f"{cheapest_line} It's also the fastest pickup. Want me to book it?"

    fastest_eta = fastest.get("pickup_eta_minutes")
    fastest_line = (
        f"{fastest.get('provider')} {fastest.get('ride_type')} is fastest at "
        f"{_format_price(fastest.get('price'), fastest.get('currency'))}"
        + (f" with a {fastest_eta}-minute pickup" if fastest_eta is not None else "")
        + "."
    )
    return (
        f"Here are your options. {cheapest_line} {fastest_line} "
        "Let me know which one you'd like, or say 'book the cheapest'."
    )


def _tool_error_reply(result: dict[str, Any], fallback_action: str) -> str:
    """Turn a failed tool-result envelope into an assistant-safe message."""
    detail = result.get("error", "an unknown error occurred")
    return (
        f"I ran into a problem while trying to {fallback_action}: {detail}. "
        "Please try again in a moment."
    )


class ChatResponder:
    """Streams the assistant's reply, delegating to the LLM or the stub simulator."""

    def __init__(self, llm_service: LLMService, publisher: StreamPublisher) -> None:
        """Wire the responder to the LLM service and the stream publisher.

        Args:
            llm_service: The live tool-calling backend (used when configured).
            publisher: Used by the stub path to push side-channel SSE events
                (quote snapshot, route map, booking created).
        """
        self.llm = llm_service
        self.publisher = publisher

    async def stream_reply(
        self,
        context: ConversationContext,
        user_message: str,
        *,
        tool_context: ToolContext,
        db: AsyncSession,
    ) -> AsyncIterator[str]:
        """Stream the assistant's reply for one turn, one token chunk at a time.

        Delegates to the live LLM tool-calling loop when a live endpoint is
        configured, otherwise to the no-key stub simulator. Both yield token
        strings, keeping the SSE plumbing in the route unchanged.

        Args:
            context: The loaded conversation context for this chat session.
            user_message: The raw text the user just sent.
            tool_context: Trusted identity plus current ride-state links and
                optional GPS pickup for this turn.
            db: Active database session, used by the stub path to persist the
                chat session's quote/booking links so it can resume mid-flow.

        Yields:
            Successive token chunks of the reply, in order.
        """
        if self.llm.enabled():
            # Live path: the model drives the tools; just relay its token stream.
            async for chunk in self.llm.stream_reply(
                context, user_message, tool_context=tool_context, db=db
            ):
                yield chunk
            return

        # Stub path: simulate intent, drive the real backend tools, then stream
        # the composed reply token by token.
        logger.info("stub turn start: user_msg=%s", truncate(user_message))
        reply = await self._run_stub_turn(context, user_message, tool_context, db)
        logger.info("stub final reply: %s", truncate(reply))
        delay = _token_delay_seconds()
        for chunk in _tokenize(reply):
            yield chunk
            if delay:
                await asyncio.sleep(delay)

    # -----------------------------------------------------------------------
    # Stub intent simulator (no-key path)
    # -----------------------------------------------------------------------

    async def _run_stub_turn(
        self,
        context: ConversationContext,
        user_message: str,
        ctx: ToolContext,
        db: AsyncSession,
    ) -> str:
        """Pick an intent from the message + ride state and drive the right tools.

        The stub infers the active quote session / booking from the chat session's
        persisted links (carried on ``ctx``) so it can continue a flow across
        turns. It mirrors the LLM's allowed actions and goes through the same
        tools registry, so the boundary guarantees hold in both modes.
        """
        text = user_message.strip().lower()

        has_quote_session = ctx.quote_session_id is not None
        has_booking = ctx.booking_id is not None

        # 1) Cancellation takes priority over everything else.
        if _mentions(text, ("cancel", "never mind", "nevermind", "stop")):
            return await self._stub_cancel(ctx)

        # 2) Explicit confirmation of a pending booking (Requirement 5.6).
        if has_booking and _is_affirmative(text):
            return await self._stub_confirm_booking(ctx)

        # 3) Booking intent against an existing search (select / book).
        if has_quote_session and _mentions(text, ("book", "reserve", "confirm")):
            return await self._stub_create_booking(text, ctx, db)

        if has_quote_session and _mentions(
            text, ("select", "choose", "pick", "go with", "i'll take", "ill take")
        ):
            return await self._stub_select_quote(text, ctx, db)

        # 4) A new ride request (mentions a destination or asks to find a ride).
        if _is_ride_request(text):
            return await self._stub_start_search(user_message, ctx, db)

        # 5) Re-summarize existing quotes if the user seems to be asking about them.
        if has_quote_session and _mentions(
            text, ("quote", "option", "price", "cheap", "fast", "show")
        ):
            return await self._stub_resummarize(ctx)

        # 6) Fallback.
        return _stub_fallback(has_quote_session, has_booking)

    async def _persist_quote_link(
        self, db: AsyncSession, chat_session_id: uuid.UUID, quote_session_id: uuid.UUID | str
    ) -> None:
        """Persist the chat session's link to its active quote session."""
        repo = ChatRepository(db)
        chat_session = await repo.get_chat_session(chat_session_id)
        if chat_session is not None:
            await repo.update_session_links(
                chat_session, quote_session_id=uuid.UUID(str(quote_session_id))
            )

    async def _persist_booking_link(
        self, db: AsyncSession, chat_session_id: uuid.UUID, booking_id: uuid.UUID | str
    ) -> None:
        """Persist the chat session's link to its active booking."""
        repo = ChatRepository(db)
        chat_session = await repo.get_chat_session(chat_session_id)
        if chat_session is not None:
            await repo.update_session_links(
                chat_session, booking_id=uuid.UUID(str(booking_id))
            )

    async def _stub_start_search(
        self, message: str, ctx: ToolContext, db: AsyncSession
    ) -> str:
        """Create a quote session and fetch quotes for a new ride request.

        Uses the GPS pickup from this turn when present (Requirement 1.3);
        otherwise creates the session with the extracted dropoff address. On
        success, summarizes cheapest/fastest (Requirement 2.4).
        """
        dropoff = _extract_dropoff(message)

        create_result = await tools.create_quote_session(
            user_id=ctx.user_id,
            chat_session_id=ctx.chat_session_id,
            dropoff_address=dropoff,
            pickup_lat=ctx.pickup_lat,
            pickup_lng=ctx.pickup_lng,
        )
        if not create_result.get("success"):
            return _tool_error_reply(create_result, "start your ride search")

        session_data = create_result["data"]
        quote_session_id = session_data["id"]
        await self._persist_quote_link(db, ctx.chat_session_id, quote_session_id)

        # Push pickup/dropoff coords so the frontend renders the route map as soon
        # as the search starts, before quotes arrive.
        await self.publisher.push_route_map(
            ctx.user_id, ctx.chat_session_id, session_data
        )

        # Coordinates are always resolved by the tool layer now, so the Quote
        # Service can fetch immediately without asking the user for a location.
        fetch_result = await tools.fetch_quotes(quote_session_id)
        if not fetch_result.get("success"):
            return _tool_error_reply(fetch_result, "fetch ride quotes")

        quotes = fetch_result["data"].get("quotes", [])
        # Push the quotes so the ride panel appears before the spoken summary.
        await self.publisher.push_quote_snapshot(
            ctx.user_id, ctx.chat_session_id, quotes
        )
        summary = _summarize_quotes(quotes)
        unavailable = fetch_result["data"].get("unavailable_providers", [])
        if unavailable:
            summary += f" (Couldn't reach: {', '.join(unavailable)}.)"
        return summary

    async def _pick_quote(
        self, quote_session_id: uuid.UUID | str, text: str
    ) -> dict[str, Any]:
        """Re-fetch quotes and choose one matching the user's mention.

        Picks the cheapest available quote for a mentioned provider, or the
        overall cheapest available quote when no provider is named.

        Returns on success ``{"success": True, "quote": <quote dict>}``; on
        failure the tool error envelope from fetch_quotes.
        """
        fetch_result = await tools.fetch_quotes(quote_session_id)
        if not fetch_result.get("success"):
            return fetch_result

        quotes = [
            q for q in fetch_result["data"].get("quotes", []) if q.get("available", True)
        ]
        if not quotes:
            return {"success": False, "error": "there are no available rides to choose from"}

        mentioned = next((p for p in _KNOWN_PROVIDERS if p in text), None)
        candidates = quotes
        if mentioned:
            provider_quotes = [
                q for q in quotes if str(q.get("provider", "")).lower() == mentioned
            ]
            if provider_quotes:
                candidates = provider_quotes

        chosen = min(candidates, key=lambda q: q.get("price", float("inf")))
        return {"success": True, "quote": chosen}

    async def _stub_select_quote(
        self, text: str, ctx: ToolContext, db: AsyncSession
    ) -> str:
        """Record the user's selection via select_quote (Requirement 4.1)."""
        picked = await self._pick_quote(ctx.quote_session_id, text)
        if not picked.get("success"):
            return _tool_error_reply(picked, "find the ride you want to select")

        quote = picked["quote"]
        result = await tools.select_quote(
            ctx.quote_session_id, quote["provider"], quote["ride_type"]
        )
        if not result.get("success"):
            return _tool_error_reply(result, "select that ride")

        return (
            f"Got it — I've selected the {quote.get('provider')} {quote.get('ride_type')} "
            f"at {_format_price(quote.get('price'), quote.get('currency'))}. "
            "Say 'book it' when you're ready and I'll verify the final price before confirming."
        )

    async def _stub_create_booking(
        self, text: str, ctx: ToolContext, db: AsyncSession
    ) -> str:
        """Create a booking and verify its price, then ask for explicit confirmation.

        Never confirms here — surfaces the (possibly changed) final price and asks
        the user to approve, honoring Requirements 5.2, 5.3, and 5.6.
        """
        picked = await self._pick_quote(ctx.quote_session_id, text)
        if not picked.get("success"):
            return _tool_error_reply(picked, "find a ride to book")

        quote = picked["quote"]
        create_result = await tools.create_booking(
            user_id=ctx.user_id,
            chat_session_id=ctx.chat_session_id,
            provider=quote.get("provider"),
            ride_type=quote.get("ride_type"),
            selected_price=quote.get("price"),
            quote_session_id=ctx.quote_session_id,
            quote_id=quote.get("id"),
            currency=quote.get("currency"),
            pickup_eta_minutes=quote.get("pickup_eta_minutes"),
        )
        if not create_result.get("success"):
            return _tool_error_reply(create_result, "create your booking")

        booking = create_result["data"]
        booking_id = booking["id"]
        await self._persist_booking_link(db, ctx.chat_session_id, booking_id)

        # Push booking_created so the frontend hides ride cards immediately.
        await self.publisher.push_booking_created(
            ctx.user_id, ctx.chat_session_id, booking_id
        )

        verify_result = await tools.verify_booking(booking_id)
        if not verify_result.get("success"):
            return _tool_error_reply(verify_result, "verify the final price")

        verify = verify_result["data"]
        final_price = _format_price(verify.get("final_price"), verify.get("currency"))
        provider = booking.get("provider")
        ride_type = booking.get("ride_type")

        if verify.get("price_changed"):
            selected = _format_price(verify.get("selected_price"), verify.get("currency"))
            return (
                f"Heads up — the price for the {provider} {ride_type} changed from "
                f"{selected} to {final_price}. Reply 'yes, confirm' to book at the new "
                "price, or 'cancel' to drop it."
            )

        return (
            f"Your {provider} {ride_type} is ready to book at {final_price}. "
            "Reply 'yes, confirm' to lock it in — I won't book until you approve."
        )

    async def _stub_confirm_booking(self, ctx: ToolContext) -> str:
        """Confirm the pending booking after explicit user approval (Req 5.4, 5.6)."""
        result = await tools.confirm_booking(ctx.booking_id, confirmed=True)
        if not result.get("success"):
            return _tool_error_reply(result, "confirm your booking")

        data = result["data"]
        final_price = _format_price(data.get("final_price"), data.get("currency"))
        booking = data.get("booking", {})
        provider = booking.get("provider", "your ride")
        confirmation = data.get("provider_booking_id", "")
        replay = " (already confirmed earlier)" if data.get("idempotent_replay") else ""
        confirmation_text = f" Confirmation: {confirmation}." if confirmation else ""
        return (
            f"All set — your {provider} ride is confirmed at {final_price}{replay}."
            f"{confirmation_text} I'll keep you posted as your driver is assigned."
        )

    async def _stub_cancel(self, ctx: ToolContext) -> str:
        """Cancel the active booking if there is one, else the quote session."""
        if ctx.booking_id is not None:
            result = await tools.cancel_booking(ctx.booking_id)
            if not result.get("success"):
                return _tool_error_reply(result, "cancel your booking")
            return "Done — I've cancelled that booking. Let me know if you'd like to search again."

        if ctx.quote_session_id is not None:
            result = await tools.cancel_quote_session(ctx.quote_session_id)
            if not result.get("success"):
                return _tool_error_reply(result, "cancel your ride search")
            return "Okay, I've stopped that ride search. Just say the word to start a new one."

        return "There's nothing active to cancel right now. Want to search for a ride?"

    async def _stub_resummarize(self, ctx: ToolContext) -> str:
        """Re-fetch and summarize the current quotes for the active search."""
        fetch_result = await tools.fetch_quotes(ctx.quote_session_id)
        if not fetch_result.get("success"):
            return _tool_error_reply(fetch_result, "refresh your quotes")
        quotes = fetch_result["data"].get("quotes", [])
        await self.publisher.push_quote_snapshot(
            ctx.user_id, ctx.chat_session_id, quotes
        )
        return _summarize_quotes(quotes)


def _stub_fallback(has_quote_session: bool, has_booking: bool) -> str:
    """Compose a helpful fallback reply based on where the flow stands."""
    if has_booking:
        return (
            "I'm RidePilot. You have a booking in progress — reply 'yes, confirm' "
            "to book it, or 'cancel' to drop it."
        )
    if has_quote_session:
        return (
            "I'm RidePilot. I've got an active search going. You can say 'show "
            "options', 'select the Lyft', or 'book the cheapest'."
        )
    return (
        "I'm RidePilot, your ride assistant. Tell me where you'd like to go — for "
        "example, 'find me a ride to the airport' — and I'll compare your options."
    )
