"""Provider-agnostic LLM interface.

The agent loop speaks only this normalized format; each provider adapter
translates to its own wire format. Normalized message shapes:

    {"role": "user", "content": "<text>"}
    {"role": "assistant", "content": "<text or None>", "tool_calls": [ToolCallRequest-like dicts]}
    {"role": "tool", "results": [{"tool_call_id", "name", "content", "is_error"}]}
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from pydantic import BaseModel, Field


class ToolCallRequest(BaseModel):
    id: str
    name: str
    arguments: dict = Field(default_factory=dict)
    # The provider's original tool-call object, replayed verbatim when the
    # conversation is sent back. Some providers attach opaque fields they
    # require to see again — Gemini's thinking models reject a follow-up whose
    # tool calls are missing their `thought_signature`. Rebuilding the call
    # from name+arguments alone silently drops those.
    raw: dict | None = None


class LLMResponse(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    # Normalized: end_turn | tool_use | max_tokens | refusal
    stop_reason: str = "end_turn"
    usage: dict = Field(default_factory=dict)


class TransientLLMError(Exception):
    """Retryable failure (rate limit, connection error, 5xx).

    ``retry_after`` carries the provider's Retry-After hint (seconds) when one
    was sent — free-tier per-minute token windows need much longer waits than
    exponential backoff alone would produce.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderNotConfiguredError(Exception):
    """Raised when the selected provider has no API key configured."""


class LLMProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    async def complete(
        self, system: str, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        """Run one model turn. ``tools`` entries: {name, description, input_schema}."""

    async def complete_streaming(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        on_delta: Callable[[str], None],
    ) -> LLMResponse:
        """Run one model turn, reporting reply text through ``on_delta``.

        The default implementation is deliberately non-streaming: it runs the
        normal call and delivers the reply as a single delta. That keeps the
        agent loop on one code path regardless of provider, so a provider that
        cannot stream degrades to "the text arrives all at once" rather than
        needing a branch at the call site. Adapters that can stream override it.
        """
        response = await self.complete(system, messages, tools)
        if response.text:
            on_delta(response.text)
        return response

    @property
    def is_configured(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        """Human-facing provider name for the UI.

        Distinct from ``name``, which identifies the *adapter*. The OpenAI
        adapter drives several vendors via ``OPENAI_BASE_URL``, so showing
        "openai" when the requests go to DeepSeek would be misleading.
        """
        return self.name
