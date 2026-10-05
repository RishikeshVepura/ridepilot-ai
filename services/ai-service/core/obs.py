"""Logging and Langfuse helpers for AI Service observability.

The goal is plain, readable logs that show what the assistant is doing each turn:
the tools (backend API calls) it requests, the arguments, and the responses —
plus the LLM's final reply. No metrics, no external services; just structured
log lines on stdout that show up in ``docker compose logs ai-service``. When
configured, Langfuse adds structured agent, generation, and tool traces without
changing the behavior of the ride flow.

:func:`truncate` keeps individual log lines bounded so a large quote list or
tool payload doesn't flood the logs while still showing the gist.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

# Max characters of any single logged value (request/response bodies, replies).
LOG_VALUE_MAX_CHARS = 2000

logger = logging.getLogger("ai-service.observability")


def langfuse_configured() -> bool:
    """Whether the two credentials required for Langfuse export are present."""
    return bool(
        os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    )


def configure_langfuse() -> None:
    """Enable automatic LiteLLM generations when Langfuse is configured.

    The Langfuse SDK and LiteLLM callback both read their endpoints and
    credentials from the process environment. Missing credentials intentionally
    leave tracing disabled, so observability can never be a prerequisite for
    serving chat requests.
    """
    if not langfuse_configured():
        logger.info(
            "Langfuse disabled: LANGFUSE_PUBLIC_KEY or "
            "LANGFUSE_SECRET_KEY is missing"
        )
        return

    import litellm
    from langfuse import get_client

    callbacks = list(litellm.callbacks or [])
    if "langfuse_otel" not in callbacks:
        callbacks.append("langfuse_otel")
        litellm.callbacks = callbacks

    # Initialize the environment-configured singleton before requests arrive.
    get_client()
    logger.info("Langfuse observability enabled")


def shutdown_langfuse() -> None:
    """Flush queued observations during graceful application shutdown."""
    if not langfuse_configured():
        return

    from langfuse import get_client

    get_client().shutdown()


def truncate(value: Any, limit: int = LOG_VALUE_MAX_CHARS) -> str:
    """Render a value as a compact, length-bounded string for logging.

    Dicts/lists are JSON-encoded (falling back to ``str`` for anything not
    serializable); strings are used as-is. Output longer than ``limit`` is cut
    with a short note of how many characters were dropped.

    Args:
        value: The value to render (e.g. a request body or tool result).
        limit: Maximum characters to emit before truncating.

    Returns:
        A single-line string safe to drop into a log message.
    """
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) > limit:
        return f"{text[:limit]}… (+{len(text) - limit} more chars)"
    return text
