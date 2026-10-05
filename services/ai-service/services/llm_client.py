"""Thin LiteLLM transport adapter used by the ride-agent loop.

Keeping provider SDK details (model prefixes, endpoint selection, timeouts, and
stream construction) here prevents them from spreading into agent logic.
"""

from __future__ import annotations

from typing import Any

from core.llm_config import LLMSettings


async def create_llm_stream(
    settings: LLMSettings,
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    *,
    observability_metadata: dict[str, Any] | None = None,
) -> Any:
    """Create one bounded, no-retry stream for the selected provider."""
    if not settings.enabled:
        raise RuntimeError(f"{settings.provider} is not configured")

    # Imported lazily so no-key stub mode does not require LiteLLM at import time.
    from litellm import acompletion

    request: dict[str, Any] = {
        "model": settings.model,
        "messages": messages,
        "tools": tool_schemas,
        "stream": True,
        "max_tokens": settings.max_tokens,
        "temperature": settings.temperature,
        "timeout": settings.timeout_seconds,
        # A loop round maps to exactly one provider request. Retrying would make
        # quota consumption less predictable.
        "num_retries": 0,
    }
    if observability_metadata:
        request["metadata"] = observability_metadata
    if settings.api_key is not None:
        request["api_key"] = settings.api_key
    if settings.api_base is not None:
        request["api_base"] = settings.api_base
    return await acompletion(**request)
