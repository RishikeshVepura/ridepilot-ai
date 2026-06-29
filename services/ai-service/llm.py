"""OpenAI client and streaming tool-call loop for the AI Service.

This module implements the *live* assistant: when a live endpoint is configured —
an OpenAI API key, or an OpenAI-compatible base URL such as a local Ollama
server (OPENAI_BASE_URL) — it drives a streaming chat completion with tool
calling, where the model decides intent and requests named backend tools, and
this loop executes those tools (via the shared registry in tools.py) and feeds
the structured results back until the model produces a final spoken reply. The
no-key fallback lives in responder.py; :func:`llm_enabled` is the switch
responder.py uses to choose between them.

Boundary note (Requirement 9): the model never executes anything itself. It can
only emit a tool *request*; this loop is the backend that "validates state and
executes" by calling the Quote/Booking services through tools.TOOL_DISPATCH
(Requirement 9.4). Identity (user_id, chat_session_id) is injected here from the
trusted :class:`tools.ToolContext`, never taken from the model, and confirmation
still flows through confirm_booking which the Booking Service guards (9.3).

The public seam is :func:`stream_llm_reply`, an async iterator of token strings
matching responder.stream_assistant_reply so routes.py is agnostic to which
backend produced the reply.
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

import repository
import tools
from events import push_quote_snapshot
from obs import truncate
from schemas import ConversationContext
from tools import ToolContext

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
# overridable via SYSTEM_PROMPT_PATH and defaults to prompts/system_prompt.md
# next to this module.
SYSTEM_PROMPT_PATH_ENV = "SYSTEM_PROMPT_PATH"
_DEFAULT_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"

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

    Returns:
        The system prompt text.
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

    Returns:
        The usable API key, or None.
    """
    raw = os.getenv(OPENAI_API_KEY_ENV)
    print(f"Open APi ")
    if raw is None:
        return None
    key = raw.strip()
    if not key or key == OPENAI_API_KEY_PLACEHOLDER:
        return None
    return key


def _base_url() -> str | None:
    """Return the configured OpenAI-compatible base URL, or None when unset.

    Returns:
        The endpoint base URL (e.g. ``http://ollama:11434/v1``), or None to use
        the SDK's default OpenAI endpoint.
    """
    raw = os.getenv(OPENAI_BASE_URL_ENV)
    if raw is None:
        return None
    url = raw.strip()
    return url or None


def llm_enabled() -> bool:
    """Report whether the live LLM path should be used.

    Enabled when EITHER a usable API key is configured (paid/hosted OpenAI or a
    keyed compatible provider) OR a custom base URL is set (a local/open-source
    server such as Ollama that needs no key). When neither is present the service
    falls back to the no-key stub intent simulator in responder.py.

    Returns:
        True when a live LLM endpoint is configured; False to use the stub.
    """
    return _api_key() is not None or _base_url() is not None


def llm_status() -> dict[str, Any]:
    """Summarize the current LLM configuration for diagnostics.

    Safe to expose: reports whether the live path is active and which model /
    endpoint it targets, but never returns the API key itself — only whether one
    is configured.

    Returns:
        A dict with ``mode`` ("live" or "stub"), the ``model`` and ``base_url``
        in use (None when not applicable), and ``api_key_configured``.
    """
    enabled = llm_enabled()
    return {
        "mode": "live" if enabled else "stub",
        "model": _model_name() if enabled else None,
        "base_url": _base_url(),
        "api_key_configured": _api_key() is not None,
    }


def _model_name() -> str:
    """Resolve the chat model name from the environment.

    Returns:
        The configured OPENAI_MODEL, or the default small model.
    """
    return os.getenv(OPENAI_MODEL_ENV, DEFAULT_OPENAI_MODEL)


def _max_tokens() -> int:
    """Resolve the max completion length (tokens) from the environment.

    Returns:
        The configured OPENAI_MAX_TOKENS, or the default; falls back to the
        default for unset/invalid/non-positive values.
    """
    raw = os.getenv(OPENAI_MAX_TOKENS_ENV)
    if raw is None:
        return DEFAULT_MAX_TOKENS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_TOKENS
    return value if value > 0 else DEFAULT_MAX_TOKENS


def _temperature() -> float:
    """Resolve the sampling temperature from the environment.

    Returns:
        The configured OPENAI_TEMPERATURE, or the default; falls back to the
        default for unset/invalid/negative values.
    """
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

    Targets a custom OpenAI-compatible endpoint when OPENAI_BASE_URL is set
    (e.g. a local Ollama server), otherwise OpenAI's default endpoint. Local
    servers ignore the API key but the SDK requires a non-empty string, so a
    placeholder is supplied when only a base URL is configured.

    Returns:
        The shared AsyncOpenAI client instance.

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
    context: ConversationContext, user_message: str
) -> list[dict[str, Any]]:
    """Assemble the chat messages: system prompt + history + new user turn.

    The stored conversation history (oldest first) is replayed directly since
    message roles already match the chat API's "user"/"assistant" values. The
    just-received user message is appended last.

    Args:
        context: The loaded conversation context for this chat session.
        user_message: The raw text the user just sent.

    Returns:
        The ordered list of chat message dicts for the completion request.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in context.messages:
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": user_message})
    return messages


def _invocation_kwargs(name: str, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Merge model-supplied tool arguments with trusted, server-injected ones.

    Identity and context the model must not control (user_id, chat_session_id)
    are injected here from the trusted ToolContext for the tools that need them,
    overriding anything the model may have tried to supply (Requirement 9.4).

    Args:
        name: The tool name the model requested.
        arguments: The arguments object the model produced (already parsed).
        ctx: The trusted tool context for this turn.

    Returns:
        The keyword arguments to invoke the dispatched tool with.
    """
    kwargs = dict(arguments)
    if name == "create_quote_session":
        kwargs["user_id"] = ctx.user_id
        kwargs["chat_session_id"] = ctx.chat_session_id
        # Use the GPS pickup the frontend sent this turn when the model didn't
        # specify a pickup itself (Requirement 1.3 — "use my current location").
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
        # Link the booking to the active search when the model didn't pass it.
        if not kwargs.get("quote_session_id") and ctx.quote_session_id is not None:
            kwargs["quote_session_id"] = str(ctx.quote_session_id)

    # Backstop for weaker models: fill the session/booking id from the trusted
    # context when the model left it out but the conversation already has one.
    if (
        name in _QUOTE_SESSION_TOOLS
        and not kwargs.get("quote_session_id")
        and ctx.quote_session_id is not None
    ):
        kwargs["quote_session_id"] = str(ctx.quote_session_id)
    if (
        name in _BOOKING_TOOLS
        and not kwargs.get("booking_id")
        and ctx.booking_id is not None
    ):
        kwargs["booking_id"] = str(ctx.booking_id)
    return kwargs


async def _execute_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Dispatch a single model-requested tool call and return its result envelope.

    Unknown tool names and bad argument shapes are turned into structured error
    envelopes rather than exceptions so the loop can hand them back to the model.

    Args:
        name: The tool name the model requested.
        arguments: The parsed arguments object from the model.
        ctx: The trusted tool context for this turn.

    Returns:
        The tool's result envelope (success or error dict).
    """
    func = tools.TOOL_DISPATCH.get(name)
    if func is None:
        return {"success": False, "tool": name, "error": f"unknown tool '{name}'"}
    try:
        return await func(**_invocation_kwargs(name, arguments, ctx))
    except TypeError as exc:
        # The model supplied arguments that don't match the tool signature.
        return {
            "success": False,
            "tool": name,
            "error": f"invalid arguments for tool '{name}': {exc}",
        }


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    """Parse a tool call's JSON argument string into a dict.

    Args:
        raw: The accumulated JSON arguments string from the model (may be empty).

    Returns:
        The parsed arguments, or an empty dict when absent/unparseable.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _persist_state_link(
    db: AsyncSession | None,
    chat_session_id: uuid.UUID,
    *,
    quote_session_id: uuid.UUID | None = None,
    booking_id: uuid.UUID | None = None,
) -> None:
    """Persist the chat session's link to its quote session / booking.

    Mirrors the stub path so the live LLM flow also resumes mid-flow across turns.
    This is the AI Service's own chat-state link only — ride state stays
    authoritative upstream (Requirement 9.5). Best-effort: a persistence hiccup
    must never break the turn.
    """
    if db is None:
        return
    try:
        chat_session = await repository.get_chat_session(db, chat_session_id)
        if chat_session is None:
            return
        await repository.update_session_links(
            db,
            chat_session,
            quote_session_id=quote_session_id,
            booking_id=booking_id,
        )
    except Exception:  # noqa: BLE001 - link persistence must not fail the turn
        logger.exception(
            "Failed to persist ride-state link for chat session %s", chat_session_id
        )


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


async def stream_llm_reply(
    context: ConversationContext,
    user_message: str,
    *,
    tool_context: ToolContext,
    db: AsyncSession | None = None,
) -> AsyncIterator[str]:
    """Stream the live LLM reply for one turn, executing tool calls as needed.

    Runs the streaming tool-calling loop: each round streams the model's text
    deltas as token strings while accumulating any tool calls; when a round ends
    with tool calls, each is executed through the backend tool registry and its
    result appended as a ``tool`` message, then the model is re-invoked. The loop
    ends when the model returns text with no further tool calls, or when
    :data:`MAX_TOOL_ROUNDS` is reached.

    Ride-state ids returned by tools (a created quote session or booking) are
    captured into a live copy of the tool context and persisted to the chat
    session, so later tool calls — this turn and on subsequent turns — can reuse
    them even when a smaller model forgets to thread the id through. See
    :func:`_invocation_kwargs` for the injection that uses them.

    Args:
        context: The loaded conversation context for this chat session.
        user_message: The raw text the user just sent.
        tool_context: Trusted identity/state injected into tool invocations.
        db: Active database session for persisting ride-state links (optional).

    Yields:
        Successive token chunks of the assistant's reply, in order.
    """
    client = _get_client()
    model = _model_name()
    messages = _build_messages(context, user_message)
    # Mutable copy updated as tools reveal ride-state ids this turn.
    live_ctx = tool_context
    logger.info(
        "llm turn start: model=%s history_msgs=%d user_msg=%s",
        model,
        len(messages),
        truncate(user_message),
    )

    for _ in range(MAX_TOOL_ROUNDS):
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
        except Exception as exc:  # noqa: BLE001 - surface any SDK/transport error cleanly
            # Log the full traceback so a model outage is visible in the logs,
            # then surface a clear, user-facing error rather than crashing the
            # SSE stream. Connection/timeout failures get a more specific hint.
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

        # No tool calls this round means the model produced its final answer.
        if not tool_calls:
            logger.info("llm final reply: %s", truncate("".join(text_parts)))
            return

        # Record the assistant's tool-call message exactly as the API expects,
        # then execute each tool and append its result as a `tool` message.
        ordered = [tool_calls[i] for i in sorted(tool_calls)]
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
                "llm requested tool: %s  args=%s",
                call["name"],
                truncate(arguments),
            )
            result = await _execute_tool(call["name"], arguments, live_ctx)
            logger.info("tool result: %s → %s", call["name"], truncate(result))

            # As soon as quotes are fetched, push them to the session's SSE
            # stream so the ride panel appears right away — before this turn's
            # spoken summary finishes streaming.
            if (
                call["name"] == "fetch_quotes"
                and isinstance(result, dict)
                and result.get("success")
            ):
                quotes = (result.get("data") or {}).get("quotes") or []
                await push_quote_snapshot(
                    live_ctx.user_id, live_ctx.chat_session_id, quotes
                )

            # Remember any ride-state ids the tool revealed so later calls (this
            # turn and next) can reuse them even if the model doesn't echo them.
            new_qs, new_bk = _extract_state_ids(call["name"], result)
            if new_qs and str(live_ctx.quote_session_id or "") != new_qs:
                live_ctx = replace(live_ctx, quote_session_id=uuid.UUID(new_qs))
                await _persist_state_link(
                    db, live_ctx.chat_session_id,
                    quote_session_id=live_ctx.quote_session_id,
                )
            if new_bk and str(live_ctx.booking_id or "") != new_bk:
                live_ctx = replace(live_ctx, booking_id=uuid.UUID(new_bk))
                await _persist_state_link(
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
        # Loop back: re-invoke the model with the tool results in context.

    # Reached the round cap without a final text answer — close out politely.
    yield "I've gathered what I can for now. Could you let me know how you'd like to proceed?"
