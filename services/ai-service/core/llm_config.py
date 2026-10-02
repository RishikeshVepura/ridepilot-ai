"""Validated, bounded configuration for direct LiteLLM providers.

This module is deliberately the only place that knows provider environment
variable names.  The ride-agent loop receives an :class:`LLMSettings` value
instead of reaching into process environment while it is running.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

LLM_PROVIDER_ENV = "LLM_PROVIDER"
LLM_MAX_TOKENS_ENV = "LLM_MAX_TOKENS"
LLM_TEMPERATURE_ENV = "LLM_TEMPERATURE"

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_MODEL_ENV = "GEMINI_MODEL"
OLLAMA_MODEL_ENV = "OLLAMA_MODEL"
OLLAMA_BASE_URL_ENV = "OLLAMA_BASE_URL"

DEFAULT_GEMINI_MODEL = "gemini/gemini-3.5-flash-lite"
ALLOWED_GEMINI_MODELS = frozenset(
    {DEFAULT_GEMINI_MODEL, "gemini/gemini-3.5-flash"}
)
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
DEFAULT_OLLAMA_BASE_URL = "http://host.docker.internal:11434"

DEFAULT_MAX_TOKENS = 128
HARD_MAX_TOKENS_PER_CALL = 256
# Gemini 3 models recommend their default temperature of 1.0.  Lower values
# can degrade their reasoning and, in some cases, cause looping behavior.
DEFAULT_GEMINI_TEMPERATURE = 1.0
DEFAULT_OLLAMA_TEMPERATURE = 0.3
LLM_REQUEST_TIMEOUT_SECONDS = 30
MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS_PER_ROUND = 3
MAX_TOOL_RESULT_CHARS = 2_000


@dataclass(frozen=True)
class LLMSettings:
    """A safe, fully-resolved direct-provider configuration."""

    provider: str
    api_key: str | None
    model: str
    api_base: str | None
    max_tokens: int
    temperature: float
    timeout_seconds: int = LLM_REQUEST_TIMEOUT_SECONDS

    @property
    def enabled(self) -> bool:
        """Whether the selected provider has enough configuration to run."""
        if self.provider == "gemini":
            return self.api_key is not None
        if self.provider == "ollama":
            return self.api_base is not None
        return False


def _positive_int(name: str, default: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return min(value, maximum) if value > 0 else default


def _nonnegative_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _provider() -> str:
    requested = os.getenv(LLM_PROVIDER_ENV, "gemini").strip().lower()
    return requested if requested in {"gemini", "ollama"} else "gemini"


def _gemini_settings() -> LLMSettings:
    raw_key = os.getenv(GEMINI_API_KEY_ENV)
    api_key = raw_key.strip() if raw_key else None
    requested_model = os.getenv(GEMINI_MODEL_ENV, DEFAULT_GEMINI_MODEL).strip()
    model = (
        requested_model
        if requested_model in ALLOWED_GEMINI_MODELS
        else DEFAULT_GEMINI_MODEL
    )
    return LLMSettings(
        provider="gemini",
        api_key=api_key or None,
        model=model,
        api_base=None,
        max_tokens=_positive_int(
            LLM_MAX_TOKENS_ENV,
            DEFAULT_MAX_TOKENS,
            HARD_MAX_TOKENS_PER_CALL,
        ),
        temperature=_nonnegative_float(
            LLM_TEMPERATURE_ENV,
            DEFAULT_GEMINI_TEMPERATURE,
        ),
    )


def _ollama_settings() -> LLMSettings:
    model = os.getenv(OLLAMA_MODEL_ENV, DEFAULT_OLLAMA_MODEL).strip()
    raw_base_url = os.getenv(OLLAMA_BASE_URL_ENV, DEFAULT_OLLAMA_BASE_URL)
    api_base = raw_base_url.strip() if raw_base_url else None
    return LLMSettings(
        provider="ollama",
        api_key=None,
        model=f"ollama/{model or DEFAULT_OLLAMA_MODEL}",
        api_base=api_base or None,
        max_tokens=_positive_int(
            LLM_MAX_TOKENS_ENV,
            DEFAULT_MAX_TOKENS,
            HARD_MAX_TOKENS_PER_CALL,
        ),
        temperature=_nonnegative_float(
            LLM_TEMPERATURE_ENV,
            DEFAULT_OLLAMA_TEMPERATURE,
        ),
    )


def load_llm_settings() -> LLMSettings:
    """Load a safe provider configuration without reading any environment file.

    Process environment may be injected by the container/runtime. Gemini models
    are allow-listed and all providers receive the same output cap.
    """
    return _ollama_settings() if _provider() == "ollama" else _gemini_settings()
