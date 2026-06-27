"""OpenAI client and streaming tool-call loop for the AI Service.

This module implements the *live* assistant: when an OpenAI API key is present it
drives a streaming chat completion with tool calling, where the model decides
intent and requests named backend tools, and this loop executes those tools (via
the shared registry in tools.py) and feeds the structured results back until the
model produces a final spoken reply. The no-key fallback lives in responder.py;
:func:`llm_enabled` is the switch responder.py uses to choose between them.

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
import os
from collections.abc import AsyncIterator
from typing import Any

import tools
from schemas import ConversationContext
from tools import ToolContext

# Env var holding the OpenAI key. The placeholder shipped in .env.example must be
# treated as "no key" so a freshly cloned repo runs in stub mode out of the box.
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_API_KEY_PLACEHOLDER = "your-openai-api-key-here"

# Model is configurable; default to a small, inexpensive tool-calling model.
OPENAI_MODEL_ENV = "OPENAI_MODEL"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

# Hard cap on tool-call rounds per turn so a misbehaving model cannot loop
# forever calling tools without ever producing a final answer.
MAX_TOOL_ROUNDS = 5

# The system prompt: defines RidePilot's role and, critically, the tool-calling
# rules that keep the model inside its boundaries (Requirement 9) and enforce the
# product rules (explicit confirmation 5.6, cheapest/fastest summary 2.4).
SYSTEM_PROMPT = (
    "You are RidePilot, a friendly AI ride assistant. You help users search for "
    "rides in natural language, compare options across providers, monitor prices, "
    "and complete a booking.\n\n"
    "You act ONLY by calling the provided tools. You must never claim to have "
    "booked, confirmed, selected, or fetched anything unless the corresponding "
    "tool returned a successful result. You cannot access databases or providers "
    "directly — the tools are your only way to act.\n\n"
    "Flow guidance:\n"
    "- When both a pickup and dropoff are known, call create_quote_session, then "
    "fetch_quotes.\n"
    "- After quotes are fetched, ALWAYS summarize the cheapest option and the "
    "fastest option (by pickup ETA), naming provider, ride type, price, and ETA.\n"
    "- When the user picks an option, call select_quote.\n"
    "- To book, call create_booking then verify_booking. If verify shows the price "
    "changed, tell the user the new price and ask them to approve it.\n"
    "- NEVER call confirm_booking until the user has explicitly approved in their "
    "latest message (e.g. 'yes, confirm it'). Confirmation is never implicit.\n"
    "- Use cancel_quote_session or cancel_booking when the user wants to cancel.\n\n"
    "If a tool returns an error, explain the problem plainly and suggest a next "
    "step. Keep replies concise and conversational since they may be read aloud."
)

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
    if raw is None:
        return None
    key = raw.strip()
    if not key or key == OPENAI_API_KEY_PLACEHOLDER:
        return None
    return key


def llm_enabled() -> bool:
    """Report whether the live OpenAI path should be used.

    Returns:
        True when a usable API key is configured; False to fall back to the stub
        intent simulator in responder.py.
    """
    return _api_key() is not None


def _model_name() -> str:
    """Resolve the chat model name from the environment.

    Returns:
        The configured OPENAI_MODEL, or the default small model.
    """
    return os.getenv(OPENAI_MODEL_ENV, DEFAULT_OPENAI_MODEL)


def _get_client() -> Any:
    """Construct (once) and return the AsyncOpenAI client.

    Returns:
        The shared AsyncOpenAI client instance.

    Raises:
        RuntimeError: If called with no usable API key configured.
    """
    global _client
    if _client is None:
        key = _api_key()
        if key is None:
            raise RuntimeError("OpenAI API key is not configured")
        # Imported lazily so the module loads even where the SDK is unused.
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(api_key=key)
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
    elif name == "create_booking":
        kwargs["user_id"] = ctx.user_id
        kwargs["chat_session_id"] = ctx.chat_session_id
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


async def stream_llm_reply(
    context: ConversationContext,
    user_message: str,
    *,
    tool_context: ToolContext,
) -> AsyncIterator[str]:
    """Stream the live LLM reply for one turn, executing tool calls as needed.

    Runs the streaming tool-calling loop: each round streams the model's text
    deltas as token strings while accumulating any tool calls; when a round ends
    with tool calls, each is executed through the backend tool registry and its
    result appended as a ``tool`` message, then the model is re-invoked. The loop
    ends when the model returns text with no further tool calls, or when
    :data:`MAX_TOOL_ROUNDS` is reached.

    Args:
        context: The loaded conversation context for this chat session.
        user_message: The raw text the user just sent.
        tool_context: Trusted identity/state injected into tool invocations.

    Yields:
        Successive token chunks of the assistant's reply, in order.
    """
    client = _get_client()
    model = _model_name()
    messages = _build_messages(context, user_message)

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
        except Exception:  # noqa: BLE001 - surface any SDK/transport error cleanly
            # Never let an LLM/transport failure crash the SSE stream; emit a
            # short apology token instead so the turn completes gracefully.
            yield "Sorry, I hit a problem reaching the assistant service. Please try again."
            return

        # No tool calls this round means the model produced its final answer.
        if not tool_calls:
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
            result = await _execute_tool(call["name"], arguments, tool_context)
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
