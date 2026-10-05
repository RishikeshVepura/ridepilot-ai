"""LiteLLM provider-agnostic streaming tool-call loop for the AI Service.

This module implements the *live* assistant: when a live endpoint is configured —
the selected provider is configured — :class:`LLMService` drives a streaming chat
completion
with tool calling, where the model decides intent and requests named backend
tools, and this loop executes those tools (via the shared registry in
tools.tools) and feeds the structured results back until the model produces a
final spoken reply. The no-key fallback lives in services.responder;
:func:`llm_enabled` is the switch the responder uses to choose between them.

Boundary note (Requirement 9): the model never executes anything itself. It can
only emit a tool *request*; this loop is the backend that "validates state and
executes" by calling the Quote/Booking services through tools.TOOL_DISPATCH
(Requirement 9.4). Identity (user_id, chat_session_id) is injected here from the
trusted :class:`tools.ToolContext`, never taken from the model, and confirmation
still flows through confirm_booking which the Booking Service guards (9.3).

The public seam is :meth:`LLMService.stream_reply`, an async iterator of token
strings matching ChatResponder.stream_reply so the API layer is agnostic to
which backend produced the reply. The endpoint/config helpers and the pure
message-assembly helpers are kept at module level; the class holds the injected
:class:`StreamPublisher` used to push side-channel SSE events during the loop.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from langfuse import get_client, propagate_attributes
from sqlalchemy.ext.asyncio import AsyncSession

from core.llm_config import (
    MAX_TOOL_CALLS_PER_ROUND,
    MAX_TOOL_RESULT_CHARS,
    MAX_TOOL_ROUNDS,
    LLMSettings,
    load_llm_settings,
)
from core.obs import truncate
from infra.stream_publisher import StreamPublisher
from repositories.chat_repository import ChatRepository
from schemas.chat_schemas import ConversationContext
from services.llm_client import create_llm_stream
from tools import tools
from tools.tools import ToolContext

logger = logging.getLogger("ai-service.llm")

# Tools that operate on an existing quote session / booking. When the model omits
# the id but the conversation already has one linked (carried on the live tool
# context), the backend injects it — so a weaker model that forgets to thread the
# id through still works instead of erroring.
_QUOTE_SESSION_TOOLS = frozenset(
    {"fetch_quotes", "select_quote", "cancel_quote_session"}
)
_BOOKING_TOOLS = frozenset(
    {"verify_booking", "confirm_booking", "cancel_booking", "get_booking_events"}
)

# The system prompt defines RidePilot's role and the tool-calling rules that keep
# the model inside its boundaries (Requirement 9) and enforce the product rules
# (explicit confirmation 5.6, cheapest/fastest summary 2.4). It lives in an
# external file so it can be edited without touching code; the path is
# overridable via SYSTEM_PROMPT_PATH and defaults to prompts/system_prompt.md at
# the service root.
SYSTEM_PROMPT_PATH_ENV = "SYSTEM_PROMPT_PATH"
_DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.md"

# Minimal safety net used only if the prompt file is missing/unreadable, so the
# service still behaves sanely rather than sending an empty system message.
_FALLBACK_SYSTEM_PROMPT = (
    "You are RidePilot, a friendly AI ride assistant. Act only by calling the "
    "provided tools, never confirm a booking without the user's explicit "
    "approval, and keep replies short and conversational."
)


def _load_system_prompt() -> str:
    """Load the system prompt from its file, falling back if unavailable.

    Reads SYSTEM_PROMPT_PATH (or the default prompts/system_prompt.md). Returns
    the file's contents when present and non-empty; otherwise logs a warning and
    returns :data:`_FALLBACK_SYSTEM_PROMPT` so a missing file never sends an
    empty system message.
    """
    path = Path(os.getenv(SYSTEM_PROMPT_PATH_ENV, str(_DEFAULT_PROMPT_PATH)))
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning(
            "Could not read system prompt at %s (%s); using fallback", path, exc
        )
        return _FALLBACK_SYSTEM_PROMPT
    if not text:
        logger.warning("System prompt at %s is empty; using fallback", path)
        return _FALLBACK_SYSTEM_PROMPT
    return text


# Loaded once at import. Edit prompts/system_prompt.md and restart (or trigger a
# reload) to pick up changes.
SYSTEM_PROMPT = _load_system_prompt()


def llm_enabled() -> bool:
    """Report whether the live LLM path should be used.

    Enabled when the selected provider is configured. Otherwise the service falls
    back to the no-key stub simulator in the responder.
    """
    return load_llm_settings().enabled


def llm_status() -> dict[str, Any]:
    """Summarize the current LLM configuration for diagnostics.

    Safe to expose: reports whether the live path is active and which model it
    targets, but never returns the API key itself — only whether one is set.
    """
    settings = load_llm_settings()
    return {
        "mode": "live" if settings.enabled else "stub",
        "model": settings.model if settings.enabled else None,
        "provider": settings.provider if settings.enabled else None,
        "api_key_configured": settings.enabled,
    }


def _build_messages(
    context: ConversationContext,
    user_message: str,
    ride_state_block: str | None = None,
) -> list[dict[str, Any]]:
    """Assemble the chat messages: system prompt + history + ride state + new turn.

    The stored conversation history (oldest first) is replayed directly since
    message roles already match the chat API's values. When a ride-state block is
    supplied it is inserted as a system message right before the new user turn —
    placed last so it is the freshest, highest-salience context the model sees.
    The just-received user message is appended last.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in context.messages:
        messages.append({"role": msg.role, "content": msg.content})
    if ride_state_block:
        messages.append({"role": "system", "content": ride_state_block})
    messages.append({"role": "user", "content": user_message})
    return messages


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    """Parse a tool call's JSON argument string into a dict.

    Returns the parsed arguments, or an empty dict when absent/unparseable.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_state_ids(
    name: str, result: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Pull a (quote_session_id, booking_id) out of a successful tool result.

    Lets the loop remember ride-state ids the model produced so subsequent tool
    calls can reuse them. Either element may be None.
    """
    if not isinstance(result, dict) or not result.get("success"):
        return None, None
    data = result.get("data")
    if not isinstance(data, dict):
        return None, None

    quote_session_id: Any = None
    booking_id: Any = None
    if name == "create_quote_session":
        quote_session_id = data.get("id")
    elif name in ("fetch_quotes", "select_quote"):
        session = data.get("session")
        if isinstance(session, dict):
            quote_session_id = session.get("id")
    elif name == "create_booking":
        booking_id = data.get("id")
    return (
        str(quote_session_id) if quote_session_id else None,
        str(booking_id) if booking_id else None,
    )


async def _dispatch_observed_tool(
    name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Execute one resolved backend tool inside a typed Langfuse observation.

    The preceding LiteLLM generation records the tool request made by the model;
    this observation records the trusted arguments RidePilot actually executes
    and the exact result envelope supplied to the next model round.
    """
    func = tools.TOOL_DISPATCH.get(name)
    observation_name = name.replace("_", "-") if func else "reject-unknown-tool"
    langfuse = get_client()

    with langfuse.start_as_current_observation(
        as_type="tool",
        name=observation_name,
        input=arguments,
        metadata={"tool_name": name},
    ) as tool_observation:
        if func is None:
            result = {
                "success": False,
                "tool": name,
                "error": f"unknown tool '{name}'",
            }
        else:
            try:
                result = await func(**arguments)
            except TypeError as exc:
                result = {
                    "success": False,
                    "tool": name,
                    "error": f"invalid arguments for tool '{name}': {exc}",
                }

        if result.get("success"):
            tool_observation.update(output=result)
        else:
            tool_observation.update(
                output=result,
                level="ERROR",
                status_message=str(result.get("error") or "Tool call failed"),
            )
        return result


class LLMService:
    """Runs the live streaming tool-calling loop against an OpenAI-compatible LLM."""

    def __init__(self, publisher: StreamPublisher) -> None:
        """Wire the service to its stream publisher.

        Args:
            publisher: Used to push side-channel SSE events (quote snapshot, route
                map, booking created) as the loop executes tools.
        """
        self.publisher = publisher

    @staticmethod
    def enabled() -> bool:
        """Whether the live LLM path is configured (see :func:`llm_enabled`)."""
        return llm_enabled()

    @staticmethod
    def status() -> dict[str, Any]:
        """LLM configuration summary (see :func:`llm_status`)."""
        return llm_status()

    async def _build_ride_state_block(self, ctx: ToolContext) -> str:
        """Build an authoritative, plain-language ride-state summary for the model.

        Reads the current linked quote session (and booking presence) so the model
        is explicitly told what the backend already knows — an active search, the
        pickup/dropoff, the search status, and whether quotes are fetched. This
        directly addresses small models re-asking for a location they already
        have. Best-effort and never raises.
        """
        header = (
            "[Current ride state — provided by the backend and authoritative. Trust "
            "this over assumptions, and do not ask again for anything already listed.]"
        )
        lines = [header]

        if ctx.quote_session_id is None and ctx.booking_id is None:
            gps = "available" if ctx.pickup_lat is not None else "not shared"
            lines.append("- No active ride search and no booking yet.")
            lines.append(f"- User GPS pickup this turn: {gps}.")
            return "\n".join(lines)

        if ctx.quote_session_id is not None:
            try:
                detail = await tools.get_quote_session(ctx.quote_session_id)
            except Exception:  # noqa: BLE001 - context building must never fail a turn
                detail = {"success": False}

            if detail.get("success"):
                data = detail.get("data") or {}
                session = data.get("session") or {}
                quotes = data.get("quotes") or []
                pickup = session.get("pickup_address") or "(set)"
                dropoff = session.get("dropoff_address") or "(set)"
                status = session.get("status") or "unknown"
                fetched = "yes" if quotes else "no"
                lines.append(f"- Active ride search: yes (id {ctx.quote_session_id}).")
                lines.append(f"- Pickup: {pickup}")
                lines.append(f"- Dropoff: {dropoff}")
                lines.append(f"- Search status: {status}")
                lines.append(f"- Quotes fetched: {fetched}")
                lines.append(
                    "- You already have the pickup and dropoff above; do NOT ask the "
                    "user for their location. If quotes are not fetched yet, call "
                    "fetch_quotes for this session now."
                )
                # List the exact provider + ride_type values so select_quote is
                # called with strings that match the stored quotes verbatim (a
                # small model otherwise reworks them, e.g. "Lyft Wait & Save" vs
                # "Wait & Save").
                if quotes:
                    lines.append(
                        "- Available options (use these EXACT provider and ride_type "
                        "values when calling select_quote — copy verbatim):"
                    )
                    for q in quotes:
                        if not q.get("available", True):
                            continue
                        provider = q.get("provider", "")
                        ride_type = q.get("ride_type", "")
                        price = q.get("price")
                        eta = q.get("pickup_eta_minutes")
                        price_str = (
                            f"${price:.2f}" if isinstance(price, (int, float)) else "n/a"
                        )
                        eta_str = f"{eta} min" if eta is not None else "n/a"
                        lines.append(
                            f'    • provider="{provider}", ride_type="{ride_type}" '
                            f"({price_str}, pickup {eta_str})"
                        )
                # Nudge the booking chain when a quote is selected but not booked.
                if status == "QUOTE_SELECTED" and ctx.booking_id is None:
                    lines.append(
                        "- A quote is SELECTED but no booking exists yet. Next action: "
                        "call create_booking, then verify_booking. Do not claim the ride "
                        "is booked until confirm_booking returns success."
                    )
            else:
                lines.append(
                    f"- Active ride search: yes (id {ctx.quote_session_id}); live "
                    "details are momentarily unavailable, but a pickup and dropoff "
                    "are already set — do not re-ask for the location."
                )

        lines.append(
            f"- Active booking: yes (id {ctx.booking_id}). To finalize after the user "
            "approves, call confirm_booking(confirmed=true); never claim it is booked "
            "until that tool returns success."
            if ctx.booking_id is not None
            else "- Booking: none yet."
        )
        return "\n".join(lines)

    def _invocation_kwargs(
        self, name: str, arguments: dict[str, Any], ctx: ToolContext
    ) -> dict[str, Any]:
        """Merge model-supplied tool arguments with trusted, server-injected ones.

        Identity and context the model must not control (user_id, chat_session_id,
        ids) are injected here from the trusted ToolContext, overriding anything
        the model may have tried to supply (Requirement 9.4).
        """
        kwargs = dict(arguments)

        # Ids are owned by the backend, never the model. Strip any *_id the model
        # supplied (it hallucinates them) and inject the authoritative values from
        # the trusted ToolContext below (Requirement 9.4/9.5).
        for model_supplied_id in (
            "user_id",
            "chat_session_id",
            "quote_session_id",
            "booking_id",
        ):
            kwargs.pop(model_supplied_id, None)

        if name == "create_quote_session":
            kwargs["user_id"] = ctx.user_id
            kwargs["chat_session_id"] = ctx.chat_session_id
            # Use the GPS pickup the frontend sent this turn when the model didn't
            # specify a pickup itself (Requirement 1.3 — "use my location").
            if (
                kwargs.get("pickup_lat") is None
                and kwargs.get("pickup_lng") is None
                and ctx.pickup_lat is not None
                and ctx.pickup_lng is not None
            ):
                kwargs["pickup_lat"] = ctx.pickup_lat
                kwargs["pickup_lng"] = ctx.pickup_lng
        elif name == "create_booking":
            kwargs["user_id"] = ctx.user_id
            kwargs["chat_session_id"] = ctx.chat_session_id
            # Always link the booking to the active search from trusted context.
            if ctx.quote_session_id is not None:
                kwargs["quote_session_id"] = str(ctx.quote_session_id)

        # Always inject the active quote session / booking id from trusted context.
        if name in _QUOTE_SESSION_TOOLS and ctx.quote_session_id is not None:
            kwargs["quote_session_id"] = str(ctx.quote_session_id)
        if name in _BOOKING_TOOLS and ctx.booking_id is not None:
            kwargs["booking_id"] = str(ctx.booking_id)
        return kwargs

    async def _execute_tool(
        self, name: str, arguments: dict[str, Any], ctx: ToolContext
    ) -> dict[str, Any]:
        """Dispatch a single model-requested tool call and return its envelope.

        Unknown tool names and bad argument shapes are turned into structured
        error envelopes rather than exceptions so the loop can hand them back to
        the model.
        """
        resolved_arguments = self._invocation_kwargs(name, arguments, ctx)
        return await _dispatch_observed_tool(name, resolved_arguments)

    async def _persist_state_link(
        self,
        db: AsyncSession | None,
        chat_session_id: uuid.UUID,
        *,
        quote_session_id: uuid.UUID | None = None,
        booking_id: uuid.UUID | None = None,
    ) -> None:
        """Persist the chat session's link to its quote session / booking.

        Mirrors the stub path so the live LLM flow also resumes mid-flow across
        turns. AI-owned chat state only — ride state stays authoritative upstream
        (Requirement 9.5). Best-effort: a persistence hiccup must never break the
        turn.
        """
        if db is None:
            return
        try:
            repo = ChatRepository(db)
            chat_session = await repo.get_chat_session(chat_session_id)
            if chat_session is None:
                return
            await repo.update_session_links(
                chat_session,
                quote_session_id=quote_session_id,
                booking_id=booking_id,
            )
        except Exception:  # noqa: BLE001 - link persistence must not fail the turn
            logger.exception(
                "Failed to persist ride-state link for chat session %s",
                chat_session_id,
            )

    async def stream_reply(
        self,
        context: ConversationContext,
        user_message: str,
        *,
        tool_context: ToolContext,
        db: AsyncSession | None = None,
    ) -> AsyncIterator[str]:
        """Stream the live LLM reply for one turn, executing tool calls as needed.

        A context manager lives inside this async generator (rather than an
        ``@observe`` decorator) so its lifetime follows actual stream consumption.
        Automatic LiteLLM generations and observed tool executions inherit this
        active agent observation.

        Yields:
            Successive token chunks of the assistant's reply, in order.
        """
        settings: LLMSettings = load_llm_settings()
        if not settings.enabled:
            # Defensive fallback: ChatResponder normally selects the stub before
            # entering this method, but never attempt a live call without a key.
            yield "⚠️ The AI model is not configured. Please try again later."
            return

        langfuse = get_client()
        emitted_parts: list[str] = []
        round_count = 0
        with langfuse.start_as_current_observation(
            as_type="agent",
            name="ridepilot.ai-turn",
            input=user_message,
            metadata={
                "provider": settings.provider,
                "model": settings.model,
                "chat_history_messages": len(context.messages),
            },
        ) as agent_observation:
            with propagate_attributes(
                user_id=tool_context.user_id,
                session_id=str(tool_context.chat_session_id),
                trace_name="ridepilot.ai-turn",
                tags=["ridepilot", "tool-calling", settings.provider],
            ):
                try:
                    async for chunk, current_round in self._stream_live_reply(
                        context,
                        user_message,
                        tool_context=tool_context,
                        db=db,
                        settings=settings,
                    ):
                        round_count = max(round_count, current_round)
                        emitted_parts.append(chunk)
                        yield chunk
                finally:
                    agent_observation.update(
                        output="".join(emitted_parts),
                        metadata={
                            "provider": settings.provider,
                            "model": settings.model,
                            "chat_history_messages": len(context.messages),
                            "rounds": round_count,
                        },
                    )

    async def _stream_live_reply(
        self,
        context: ConversationContext,
        user_message: str,
        *,
        tool_context: ToolContext,
        db: AsyncSession | None,
        settings: LLMSettings,
    ) -> AsyncIterator[tuple[str, int]]:
        """Run the existing live loop and pair each emitted chunk with its round.

        Runs the streaming tool-calling loop: each round streams the model's text
        deltas as token strings while accumulating any tool calls; when a round
        ends with tool calls, each is executed through the backend tool registry
        and its result appended as a ``tool`` message, then the model is
        re-invoked. The loop ends when the model returns text with no further tool
        calls, or when :data:`MAX_TOOL_ROUNDS` is reached.

        Ride-state ids returned by tools are captured into a live copy of the tool
        context and persisted to the chat session so later tool calls can reuse
        them even when a smaller model forgets to thread the id through.

        Yields:
            ``(text chunk, current round)`` pairs in display order.
        """
        model = settings.model
        ride_state_block = await self._build_ride_state_block(tool_context)
        messages = _build_messages(context, user_message, ride_state_block)
        # Mutable copy updated as tools reveal ride-state ids this turn.
        live_ctx = tool_context
        logger.info(
            "llm turn start: model=%s history_msgs=%d user_msg=%s",
            model,
            len(messages),
            truncate(user_message),
        )
        logger.info("ride state block:\n%s", ride_state_block)
        # Full prompt visible at DEBUG level — opt-in because it's large.
        logger.debug("full messages sent to model:\n%s", truncate(messages, limit=8000))

        round_num = 0
        for _ in range(MAX_TOOL_ROUNDS):
            round_num += 1
            logger.info("═══ TOOL ROUND %d START ═══", round_num)

            # Accumulators for this round's streamed assistant message.
            text_parts: list[str] = []
            # Tool calls arrive in fragments keyed by index; reassemble them.
            tool_calls: dict[int, dict[str, Any]] = {}

            try:
                stream = await create_llm_stream(
                    settings,
                    messages,
                    tools.TOOL_SCHEMAS,
                    observability_metadata={
                        "generation_name": "generate-response",
                        "round": round_num,
                        "tags": ["ridepilot", "tool-calling"],
                    },
                )
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    # Stream any visible text immediately.
                    if getattr(delta, "content", None):
                        text_parts.append(delta.content)
                        logger.debug("  token: %s", repr(delta.content))
                        yield delta.content, round_num

                    # Accumulate tool-call fragments across deltas.
                    for tc in getattr(delta, "tool_calls", None) or []:
                        slot = tool_calls.setdefault(
                            tc.index,
                            {"id": None, "name": "", "arguments": ""},
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
            except Exception as exc:  # noqa: BLE001 - surface any transport error cleanly
                logger.exception("LLM request failed (model=%s)", model)
                error_name = type(exc).__name__
                if "Connection" in error_name or "Timeout" in error_name:
                    yield (
                        "⚠️ Sorry — I couldn't reach the AI model service. Please "
                        "make sure it's running and try again.",
                        round_num,
                    )
                else:
                    yield (
                        "⚠️ Sorry — an error occurred while generating a response. "
                        "Please try again in a moment.",
                        round_num,
                    )
                return

            round_text = "".join(text_parts)
            if round_text:
                logger.info("══ ROUND %d TEXT OUTPUT ══\n%s", round_num, round_text)
            else:
                logger.info(
                    "══ ROUND %d TEXT OUTPUT ══ (no text, tool calls only)", round_num
                )

            # No tool calls this round means the model produced its final answer.
            if not tool_calls:
                logger.info("══ FINAL REPLY (no more tool calls) ══\n%s", round_text)
                logger.info("═══ TOOL ROUND %d END (FINAL) ═══", round_num)
                return

            # Record the assistant's tool-call message exactly as the API expects,
            # then execute each tool and append its result as a `tool` message.
            ordered = [tool_calls[i] for i in sorted(tool_calls)]
            logger.info("══ ROUND %d TOOL CALLS ══ count=%d", round_num, len(ordered))

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text_parts) or None,
                "tool_calls": [
                    {
                        "id": call["id"] or f"call_{idx}",
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"] or "{}",
                        },
                    }
                    for idx, call in enumerate(ordered)
                ],
            }
            messages.append(assistant_msg)

            for idx, call in enumerate(ordered):
                if idx >= MAX_TOOL_CALLS_PER_ROUND:
                    # Reply to every rejected call to preserve a valid tool-call
                    # transcript for the next provider request.
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"] or f"call_{idx}",
                            "name": call["name"],
                            "content": json.dumps(
                                {
                                    "success": False,
                                    "tool": call["name"],
                                    "error": "tool-call limit reached for this model round",
                                }
                            ),
                        }
                    )
                    continue
                arguments = _parse_arguments(call["arguments"])
                logger.info(
                    "  [%d/%d] TOOL CALL: %s", idx + 1, len(ordered), call["name"]
                )
                logger.info("       arguments: %s", truncate(arguments))

                result = await self._execute_tool(call["name"], arguments, live_ctx)
                logger.info("       result: %s", truncate(result))

                # As soon as quotes are fetched, push them so the ride panel
                # appears right away — before this turn's spoken summary finishes.
                if (
                    call["name"] == "fetch_quotes"
                    and isinstance(result, dict)
                    and result.get("success")
                ):
                    quotes = (result.get("data") or {}).get("quotes") or []
                    await self.publisher.push_quote_snapshot(
                        live_ctx.user_id, live_ctx.chat_session_id, quotes
                    )

                # As soon as the search is created, push the pickup/dropoff coords
                # so the frontend can render the route map before quotes arrive.
                if (
                    call["name"] == "create_quote_session"
                    and isinstance(result, dict)
                    and result.get("success")
                ):
                    await self.publisher.push_route_map(
                        live_ctx.user_id,
                        live_ctx.chat_session_id,
                        result.get("data"),
                    )

                # When a booking is created, push booking_created so the frontend
                # hides the ride cards and shows only the map + status.
                if (
                    call["name"] == "create_booking"
                    and isinstance(result, dict)
                    and result.get("success")
                ):
                    booking_id = (result.get("data") or {}).get("id")
                    await self.publisher.push_booking_created(
                        live_ctx.user_id, live_ctx.chat_session_id, booking_id
                    )

                # Remember any ride-state ids the tool revealed so later calls
                # (this turn and next) can reuse them even if the model doesn't.
                new_qs, new_bk = _extract_state_ids(call["name"], result)
                if new_qs and str(live_ctx.quote_session_id or "") != new_qs:
                    logger.info("       captured quote_session_id: %s", new_qs)
                    live_ctx = replace(live_ctx, quote_session_id=uuid.UUID(new_qs))
                    await self._persist_state_link(
                        db,
                        live_ctx.chat_session_id,
                        quote_session_id=live_ctx.quote_session_id,
                    )
                if new_bk and str(live_ctx.booking_id or "") != new_bk:
                    logger.info("       captured booking_id: %s", new_bk)
                    live_ctx = replace(live_ctx, booking_id=uuid.UUID(new_bk))
                    await self._persist_state_link(
                        db, live_ctx.chat_session_id, booking_id=live_ctx.booking_id
                    )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"] or f"call_{idx}",
                        "name": call["name"],
                        "content": truncate(
                            json.dumps(result), limit=MAX_TOOL_RESULT_CHARS
                        ),
                    }
                )

            logger.info("═══ TOOL ROUND %d END (continuing) ═══", round_num)
            # Loop back: re-invoke the model with the tool results in context.

        # Reached the round cap without a final text answer — close out politely.
        yield (
            "I've gathered what I can for now. Could you let me know how you'd like "
            "to proceed?",
            round_num,
        )
