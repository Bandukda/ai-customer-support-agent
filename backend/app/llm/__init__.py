"""Provider factory: selects the adapter based on settings.llm_provider."""

from __future__ import annotations

from ..config import Settings
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider
from .mock_provider import MockProvider
from .openai_provider import OpenAIProvider


def create_provider(settings: Settings) -> LLMProvider:
    provider = settings.llm_provider.strip().lower()
    if provider == "anthropic":
        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            max_tokens=settings.llm_max_tokens,
            effort=settings.llm_effort,
        )
    if provider == "openai":
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            max_tokens=settings.llm_max_tokens,
            base_url=settings.openai_base_url,
        )
    if provider == "mock":
        return MockProvider()
    raise ValueError(
        f"Unknown LLM_PROVIDER '{settings.llm_provider}'. Use 'anthropic', 'openai', or 'mock'."
    )
