"""OpenAI client and streaming tool-call loop for the AI Service.

This module implements the *live* assistant: when a live endpoint is configured —
an OpenAI API key, or an OpenAI-compatible base URL such as a local Ollama
server (OPENAI_BASE_URL) — :class:`LLMService` drives a streaming chat completion
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

from sqlalchemy.ext.asyncio import AsyncSession

from core.obs import truncate
from infra.stream_publisher import StreamPublisher
from repositories.chat_repository import ChatRepository
from schemas.chat_schemas import ConversationContext
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

# Env var holding the API key. The placeholder shipped in .env.example must be
# treated as "no key" so a freshly cloned repo runs in stub mode out of the box.
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_API_KEY_PLACEHOLDER = "your-openai-api-key-here"

# Optional base URL for any OpenAI-COMPATIBLE endpoint. This is the switch that
# lets the same code talk to a free local model (e.g. Ollama at
# http://localhost:11434/v1) for testing, a hosted free tier (Groq, OpenRouter),
# or paid OpenAI — without changing anything but configuration. When unset, the
# SDK targets OpenAI's default endpoint.
OPENAI_BASE_URL_ENV = "OPENAI_BASE_URL"

# Model is configurable; default to a small, inexpensive tool-calling model.
# Override with e.g. OPENAI_MODEL=llama3.1 when pointing at a local Ollama server.
OPENAI_MODEL_ENV = "OPENAI_MODEL"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

# Cap the length of any single completion so a verbose local model can't ramble
# on indefinitely. Configurable via OPENAI_MAX_TOKENS.
OPENAI_MAX_TOKENS_ENV = "OPENAI_MAX_TOKENS"
DEFAULT_MAX_TOKENS = 512

# Sampling temperature. Lower keeps replies focused and reduces rambling / odd
# templated output from smaller models. Configurable via OPENAI_TEMPERATURE.
OPENAI_TEMPERATURE_ENV = "OPENAI_TEMPERATURE"
DEFAULT_TEMPERATURE = 0.3

# Hard cap on tool-call rounds per turn so a misbehaving model cannot loop
# forever calling tools without ever producing a final answer.
MAX_TOOL_ROUNDS = 5

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

# Lazily constructed singleton client. Built once on first use so importing this
# module never fails when the SDK or key is absent.
_client: Any | None = None


def _api_key() -> str | None:
    """Return the configured OpenAI API key, or None when effectively unset.

    Empty/whitespace values and the .env.example placeholder are all treated as
    "no key" so the service transparently runs in stub mode.
    """
    raw = os.getenv(OPENAI_API_KEY_ENV)
    if raw is None:
        return None
    key = raw.strip()
    if not key or key == OPENAI_API_KEY_PLACEHOLDER:
        return None
    return key


def _base_url() -> str | None:
    """Return the configured OpenAI-compatible base URL, or None when unset."""
    raw = os.getenv(OPENAI_BASE_URL_ENV)
    if raw is None:
        return None
    url = raw.strip()
    return url or None


def llm_enabled() -> bool:
    """Report whether the live LLM path should be used.

    Enabled when EITHER a usable API key is configured OR a custom base URL is set
    (a local/open-source server such as Ollama that needs no key). When neither is
    present the service falls back to the no-key stub simulator in the responder.
    """
    return _api_key() is not None or _base_url() is not None


def llm_status() -> dict[str, Any]:
    """Summarize the current LLM configuration for diagnostics.

    Safe to expose: reports whether the live path is active and which model /
    endpoint it targets, but never returns the API key itself — only whether one
    is configured.
    """
    enabled = llm_enabled()
    return {
        "mode": "live" if enabled else "stub",
        "model": _model_name() if enabled else None,
        "base_url": _base_url(),
        "api_key_configured": _api_key() is not None,
    }


def _model_name() -> str:
    """Resolve the chat model name from the environment."""
    return os.getenv(OPENAI_MODEL_ENV, DEFAULT_OPENAI_MODEL)


def _max_tokens() -> int:
    """Resolve the max completion length (tokens) from the environment."""
    raw = os.getenv(OPENAI_MAX_TOKENS_ENV)
    if raw is None:
        return DEFAULT_MAX_TOKENS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_TOKENS
    return value if value > 0 else DEFAULT_MAX_TOKENS


def _temperature() -> float:
    """Resolve the sampling temperature from the environment."""
    raw = os.getenv(OPENAI_TEMPERATURE_ENV)
    if raw is None:
        return DEFAULT_TEMPERATURE
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TEMPERATURE
    return value if value >= 0 else DEFAULT_TEMPERATURE


def _get_client() -> Any:
    """Construct (once) and return the AsyncOpenAI client.

    Targets a custom OpenAI-compatible endpoint when OPENAI_BASE_URL is set,
    otherwise OpenAI's default endpoint. Local servers ignore the API key but the
    SDK requires a non-empty string, so a placeholder is supplied when only a base
    URL is configured.

    Raises:
        RuntimeError: If neither an API key nor a base URL is configured.
    """
    global _client
    if _client is None:
        key = _api_key()
        base_url = _base_url()
        if key is None and base_url is None:
            raise RuntimeError(
                "No LLM endpoint configured: set OPENAI_API_KEY (hosted) or "
                "OPENAI_BASE_URL (local/compatible server)"
            )
        # Imported lazily so the module loads even where the SDK is unused.
        from openai import AsyncOpenAI

        client_kwargs: dict[str, Any] = {"api_key": key or "not-needed"}
        if base_url is not None:
            client_kwargs["base_url"] = base_url
        _client = AsyncOpenAI(**client_kwargs)
    return _client


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
        func = tools.TOOL_DISPATCH.get(name)
        if func is None:
            return {"success": False, "tool": name, "error": f"unknown tool '{name}'"}
        try:
            return await func(**self._invocation_kwargs(name, arguments, ctx))
        except TypeError as exc:
            return {
                "success": False,
                "tool": name,
                "error": f"invalid arguments for tool '{name}': {exc}",
            }

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
            Successive token chunks of the assistant's reply, in order.
        """
        client = _get_client()
        model = _model_name()
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
                stream = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools.TOOL_SCHEMAS,
                    stream=True,
                    max_tokens=_max_tokens(),
                    temperature=_temperature(),
                )
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    # Stream any visible text immediately.
                    if getattr(delta, "content", None):
                        text_parts.append(delta.content)
                        logger.debug("  token: %s", repr(delta.content))
                        yield delta.content

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
                        "make sure it's running and try again."
                    )
                else:
                    yield (
                        "⚠️ Sorry — an error occurred while generating a response. "
                        "Please try again in a moment."
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
                        "content": json.dumps(result),
                    }
                )

            logger.info("═══ TOOL ROUND %d END (continuing) ═══", round_num)
            # Loop back: re-invoke the model with the tool results in context.

        # Reached the round cap without a final text answer — close out politely.
        yield "I've gathered what I can for now. Could you let me know how you'd like to proceed?"
