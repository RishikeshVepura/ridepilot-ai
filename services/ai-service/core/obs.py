"""Small logging helpers for AI Service observability.

The goal is plain, readable logs that show what the assistant is doing each turn:
the tools (backend API calls) it requests, the arguments, and the responses —
plus the LLM's final reply. No metrics, no external services; just structured
log lines on stdout that show up in ``docker compose logs ai-service``.

:func:`truncate` keeps individual log lines bounded so a large quote list or
tool payload doesn't flood the logs while still showing the gist.
"""

from __future__ import annotations

import json
from typing import Any

# Max characters of any single logged value (request/response bodies, replies).
LOG_VALUE_MAX_CHARS = 2000


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
