"""Deterministic mock provider for OFFLINE DEVELOPMENT ONLY (LLM_PROVIDER=mock).

Walks a fixed verify -> lookup -> eligibility-check script so the full stack
(SSE streaming, tool dispatch, reasoning log, UI) can be exercised without an
API key or network access. It never processes refunds and announces itself as
mock mode — do not use it for the recorded demo.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable

from .base import LLMProvider, LLMResponse, ToolCallRequest

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_ORDER_RE = re.compile(r"ORD-\d+", re.IGNORECASE)

# Word-at-a-time playback so the streaming UI can be developed and demoed
# offline. Paced to look like a real model rather than to imitate one exactly.
_MOCK_STREAM_DELAY_SECONDS = 0.03


def _split_for_playback(text: str) -> list[str]:
    """Split into whitespace-preserving chunks whose concatenation is the input."""
    return re.findall(r"\s*\S+", text)


class MockProvider(LLMProvider):
    name = "mock"
    model = "scripted-mock"

    def __init__(self) -> None:
        self._counter = 0

    @property
    def display_name(self) -> str:
        return "Mock (offline)"

    def _next_id(self) -> str:
        self._counter += 1
        return f"mock-call-{self._counter}"

    async def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        user_text = " ".join(
            m["content"] for m in messages if m["role"] == "user" and isinstance(m.get("content"), str)
        )
        email_match = _EMAIL_RE.search(user_text)
        order_match = _ORDER_RE.search(user_text)
        called = [r["name"] for m in messages if m["role"] == "tool" for r in m["results"]]

        if not email_match:
            return LLMResponse(
                text=(
                    "[mock mode] Thanks for reaching out! To get started, could you share the "
                    "email on your account and your order ID (it looks like ORD-12345)?"
                )
            )
        if "lookup_customer" not in called:
            return LLMResponse(
                text="Let me pull up your account.",
                tool_calls=[
                    ToolCallRequest(
                        id=self._next_id(),
                        name="lookup_customer",
                        arguments={"email": email_match.group(0)},
                    )
                ],
                stop_reason="tool_use",
            )
        if not order_match:
            return LLMResponse(
                text="[mock mode] I found your account. Which order ID is this about?"
            )
        if "check_refund_eligibility" not in called:
            return LLMResponse(
                text="Checking that order against our refund policy.",
                tool_calls=[
                    ToolCallRequest(
                        id=self._next_id(),
                        name="check_refund_eligibility",
                        arguments={
                            "customer_email": email_match.group(0),
                            "order_id": order_match.group(0).upper(),
                            "reason": "changed_mind",
                        },
                    )
                ],
                stop_reason="tool_use",
            )

        decision = self._last_result(messages, "check_refund_eligibility")
        outcome = decision.get("outcome", "unknown")
        summary = decision.get("summary", "")
        return LLMResponse(
            text=(
                f"[mock mode] Policy engine outcome for {order_match.group(0).upper()}: "
                f"{outcome.upper()} — {summary} (Mock mode stops here; it never processes refunds.)"
            )
        )

    async def complete_streaming(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        on_delta: Callable[[str], None],
    ) -> LLMResponse:
        response = await self.complete(system, messages, tools)
        for word in _split_for_playback(response.text or ""):
            on_delta(word)
            await asyncio.sleep(_MOCK_STREAM_DELAY_SECONDS)
        return response

    def _last_result(self, messages: list[dict], tool_name: str) -> dict:
        for message in reversed(messages):
            if message["role"] != "tool":
                continue
            for result in message["results"]:
                if result["name"] == tool_name and not result.get("is_error"):
                    try:
                        return json.loads(result["content"])
                    except json.JSONDecodeError:
                        return {}
        return {}
