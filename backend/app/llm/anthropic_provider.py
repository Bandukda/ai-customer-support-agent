"""Anthropic adapter (default provider).

SDK auto-retries are disabled (max_retries=0) on purpose: the agent loop owns
retry logic so every retry is visible in the reasoning log instead of happening
silently inside the SDK.
"""

from __future__ import annotations

import anthropic

from .base import (
    LLMProvider,
    LLMResponse,
    ProviderNotConfiguredError,
    ToolCallRequest,
    TransientLLMError,
)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int, effort: str = "") -> None:
        self.model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._effort = effort
        self._client = anthropic.AsyncAnthropic(api_key=api_key or "unset", max_retries=0)

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    @property
    def display_name(self) -> str:
        return "Anthropic"

    async def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        if not self._api_key:
            raise ProviderNotConfiguredError(
                "ANTHROPIC_API_KEY is not set. Add it to the project .env file "
                "(or set LLM_PROVIDER=openai / mock)."
            )
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            # cache_control caches the static system prompt (policy + protocol)
            # across turns; conversation history varies after it.
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": self._to_wire(messages),
            "tools": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["input_schema"],
                }
                for t in tools
            ],
        }
        if self._effort:
            kwargs["output_config"] = {"effort": self._effort}

        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            retry_after = None
            try:
                header = exc.response.headers.get("retry-after")
                retry_after = float(header) if header else None
            except (AttributeError, ValueError, TypeError):
                pass
            raise TransientLLMError(
                f"Anthropic rate limit: {exc.__class__.__name__}", retry_after=retry_after
            ) from exc
        except anthropic.AuthenticationError as exc:
            raise ProviderNotConfiguredError(
                f"Anthropic rejected the API key ({exc.__class__.__name__}). "
                "Check ANTHROPIC_API_KEY in .env."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise TransientLLMError(f"Anthropic connection error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientLLMError(f"Anthropic server error {exc.status_code}") from exc
            raise

        return self._from_wire(response)

    def _to_wire(self, messages: list[dict]) -> list[dict]:
        wire: list[dict] = []
        for message in messages:
            role = message["role"]
            if role == "user":
                wire.append({"role": "user", "content": message["content"]})
            elif role == "assistant":
                blocks: list[dict] = []
                if message.get("content"):
                    blocks.append({"type": "text", "text": message["content"]})
                for call in message.get("tool_calls", []):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": call["name"],
                            "input": call["arguments"],
                        }
                    )
                wire.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            elif role == "tool":
                wire.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": r["tool_call_id"],
                                "content": r["content"],
                                "is_error": r.get("is_error", False),
                            }
                            for r in message["results"]
                        ],
                    }
                )
        return wire

    def _from_wire(self, response) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCallRequest(id=block.id, name=block.name, arguments=dict(block.input))
                )
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        }
        return LLMResponse(
            text="\n".join(text_parts).strip() or None,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            usage=usage,
        )
