"""OpenAI adapter (LLM_PROVIDER=openai).

Also serves as the universal client for OpenAI-compatible endpoints via
OPENAI_BASE_URL — e.g. Groq (free tier), Google Gemini's compatibility
endpoint (free tier), or a local Ollama server (fully offline). Third-party
compat layers get `max_tokens`; the real OpenAI API gets the newer
`max_completion_tokens`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from urllib.parse import urlparse

import openai

from .base import (
    LLMProvider,
    LLMResponse,
    ProviderNotConfiguredError,
    ToolCallRequest,
    TransientLLMError,
)

# OpenAI-compatible endpoints we can name properly in the UI.
_HOST_LABELS = {
    "api.deepseek.com": "DeepSeek",
    "api.groq.com": "Groq",
    "generativelanguage.googleapis.com": "Gemini",
    "api.openai.com": "OpenAI",
}

# Gemini's thinking models reject a follow-up whose tool calls are missing the
# `thought_signature` they issued. We can only preserve that by replaying the
# provider's original tool-call object (ToolCallRequest.raw), which a streamed
# response never gives us — deltas carry name and arguments only. So Gemini
# stays on the non-streaming path; correctness beats a nicer-looking reply.
_NO_STREAM_HOSTS = {"generativelanguage.googleapis.com"}

# `stream_options` is an OpenAI extension. These hosts are verified to accept
# it; elsewhere (notably local Ollama) an unknown parameter can 400 the call,
# so we omit it and accept that a streamed turn reports no token usage.
_STREAM_USAGE_HOSTS = {"api.deepseek.com", "api.groq.com", "api.openai.com"}


def _retry_after_seconds(exc) -> float | None:
    """How long the provider wants us to wait.

    Groq sends a ``Retry-After`` header. Gemini sends none, but states the
    delay inside the error message ("Please retry in 32.8s"); without parsing
    it we fall back to a 1-2s backoff that is far too short for a per-minute
    window, and burn all our attempts in a few seconds.
    """
    try:
        value = exc.response.headers.get("retry-after")
        if value:
            return float(value)
    except (AttributeError, ValueError, TypeError):
        pass
    match = re.search(r"retry in ([\d.]+)", str(exc), re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _rate_limit_detail(exc) -> str:
    """The provider's own explanation, trimmed — it names the exact quota."""
    text = re.sub(r"\s+", " ", str(exc))
    match = re.search(r"(Quota exceeded for metric[^*]{0,160}|Rate limit reached[^.]{0,160})", text)
    return match.group(1).strip() if match else text[:160]


_STOP_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str, max_tokens: int, base_url: str = "") -> None:
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._client = openai.AsyncOpenAI(
            api_key=api_key or "local-no-key",
            base_url=base_url or None,
            max_retries=0,
        )

    @property
    def is_configured(self) -> bool:
        if self._api_key:
            return True
        # Local OpenAI-compatible servers (Ollama, LM Studio) need no key.
        return bool(self.base_url) and ("localhost" in self.base_url or "127.0.0.1" in self.base_url)

    @property
    def display_name(self) -> str:
        """Name the vendor actually being called, not the adapter."""
        if not self.base_url:
            return "OpenAI"
        host = urlparse(self.base_url).hostname or ""
        if host in ("localhost", "127.0.0.1"):
            return "Ollama (local)"
        for known, label in _HOST_LABELS.items():
            if host == known or host.endswith(f".{known}"):
                return label
        return host or "OpenAI"

    @property
    def supports_streaming(self) -> bool:
        host = urlparse(self.base_url).hostname or "" if self.base_url else ""
        return not any(host == known or host.endswith(f".{known}") for known in _NO_STREAM_HOSTS)

    @contextmanager
    def _translated_errors(self) -> Iterator[None]:
        """Map provider exceptions onto our normalized error types.

        Shared by the streaming and non-streaming paths so both classify a
        rate limit or a dropped connection identically — the agent loop's
        retry behaviour must not depend on which path produced the error.
        """
        try:
            yield
        except openai.RateLimitError as exc:
            raise TransientLLMError(
                f"Rate limited: {_rate_limit_detail(exc)}",
                retry_after=_retry_after_seconds(exc),
            ) from exc
        except openai.AuthenticationError as exc:
            endpoint = self.base_url or "the OpenAI API"
            raise ProviderNotConfiguredError(
                f"{endpoint} rejected the API key ({exc.__class__.__name__}). "
                "Check OPENAI_API_KEY in .env."
            ) from exc
        except openai.APIConnectionError as exc:
            hint = (
                " Is your local model server (e.g. `ollama serve`) running?"
                if self.base_url and ("localhost" in self.base_url or "127.0.0.1" in self.base_url)
                else ""
            )
            raise TransientLLMError(f"OpenAI connection error: {exc}.{hint}") from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientLLMError(f"OpenAI server error {exc.status_code}") from exc
            # Some compat providers (e.g. Groq) return 400 "failed_generation"
            # when the model emits a malformed tool call; regenerating usually
            # succeeds, so treat it as transient rather than fatal.
            if exc.status_code == 400 and "failed_generation" in str(getattr(exc, "body", "")):
                raise TransientLLMError(
                    "Model produced a malformed tool call (provider failed_generation); retrying"
                ) from exc
            raise

    def _request_kwargs(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        if not self.is_configured:
            raise ProviderNotConfiguredError(
                "OPENAI_API_KEY is not set. Add a key to the project .env file — free keys "
                "work too (Groq: console.groq.com, Gemini: aistudio.google.com, paired with "
                "OPENAI_BASE_URL) — or point OPENAI_BASE_URL at a local server like Ollama."
            )
        # Compat layers universally accept max_tokens; the real OpenAI API
        # prefers max_completion_tokens (required on newer model families).
        token_arg = (
            {"max_tokens": self._max_tokens}
            if self.base_url
            else {"max_completion_tokens": self._max_tokens}
        )
        return {
            "model": self.model,
            **token_arg,
            "messages": [{"role": "system", "content": system}, *self._to_wire(messages)],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ],
        }

    async def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        kwargs = self._request_kwargs(system, messages, tools)
        with self._translated_errors():
            response = await self._client.chat.completions.create(**kwargs)
        return self._from_wire(response)

    async def complete_streaming(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        on_delta: Callable[[str], None],
    ) -> LLMResponse:
        if not self.supports_streaming:
            return await super().complete_streaming(system, messages, tools, on_delta)

        kwargs = self._request_kwargs(system, messages, tools)
        host = urlparse(self.base_url).hostname or "" if self.base_url else "api.openai.com"
        if host in _STREAM_USAGE_HOSTS:
            kwargs["stream_options"] = {"include_usage": True}

        text_parts: list[str] = []
        # Tool calls stream in fragments keyed by index: the name arrives once,
        # the JSON arguments accumulate across many chunks.
        partial_calls: dict[int, dict] = {}
        finish_reason = "stop"
        usage: dict = {}

        with self._translated_errors():
            stream = await self._client.chat.completions.create(**kwargs, stream=True)
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = {
                        "input_tokens": chunk.usage.prompt_tokens,
                        "output_tokens": chunk.usage.completion_tokens,
                    }
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                if delta.content:
                    text_parts.append(delta.content)
                    on_delta(delta.content)
                for fragment in delta.tool_calls or []:
                    slot = partial_calls.setdefault(
                        fragment.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if fragment.id:
                        slot["id"] = fragment.id
                    if fragment.function:
                        if fragment.function.name:
                            slot["name"] = fragment.function.name
                        if fragment.function.arguments:
                            slot["arguments"] += fragment.function.arguments

        tool_calls: list[ToolCallRequest] = []
        for index in sorted(partial_calls):
            slot = partial_calls[index]
            if not slot["name"]:
                continue
            try:
                arguments = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {"_malformed_arguments": slot["arguments"]}
            # `raw` stays None: a streamed response never exposes the provider's
            # original object. Endpoints that require it back are excluded from
            # streaming via _NO_STREAM_HOSTS.
            tool_calls.append(
                ToolCallRequest(id=slot["id"] or f"call_{index}", name=slot["name"], arguments=arguments)
            )

        return LLMResponse(
            text="".join(text_parts).strip() or None,
            tool_calls=tool_calls,
            stop_reason=_STOP_REASON_MAP.get(finish_reason, "end_turn"),
            usage=usage,
        )

    def _to_wire(self, messages: list[dict]) -> list[dict]:
        wire: list[dict] = []
        for message in messages:
            role = message["role"]
            if role == "user":
                wire.append({"role": "user", "content": message["content"]})
            elif role == "assistant":
                entry: dict = {"role": "assistant", "content": message.get("content") or None}
                calls = message.get("tool_calls", [])
                if calls:
                    entry["tool_calls"] = [
                        # Replay the provider's own object when we captured it,
                        # so opaque fields (e.g. Gemini's thought_signature)
                        # survive the round trip.
                        c.get("raw")
                        or {
                            "id": c["id"],
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": json.dumps(c["arguments"]),
                            },
                        }
                        for c in calls
                    ]
                wire.append(entry)
            elif role == "tool":
                for result in message["results"]:
                    wire.append(
                        {
                            "role": "tool",
                            "tool_call_id": result["tool_call_id"],
                            "content": result["content"],
                        }
                    )
        return wire

    def _from_wire(self, response) -> LLMResponse:
        choice = response.choices[0]
        tool_calls: list[ToolCallRequest] = []
        for call in choice.message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_malformed_arguments": call.function.arguments}
            try:
                raw = call.model_dump(exclude_none=True)
            except AttributeError:
                raw = None
            tool_calls.append(
                ToolCallRequest(
                    id=call.id, name=call.function.name, arguments=arguments, raw=raw
                )
            )
        usage = {}
        if response.usage:
            usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            }
        return LLMResponse(
            text=(choice.message.content or "").strip() or None,
            tool_calls=tool_calls,
            stop_reason=_STOP_REASON_MAP.get(choice.finish_reason, "end_turn"),
            usage=usage,
        )
