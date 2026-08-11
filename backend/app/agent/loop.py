"""The agent loop: raw function calling with visible retries and guardrails.

One ``run_turn`` call handles one user message:

    user message -> [LLM call -> tool calls -> tool results]* -> final reply

Every step emits an AgentEvent so the admin dashboard shows the full reasoning
trace in real time. Failure handling:

- Transient LLM errors (rate limit / connection / 5xx) retry with exponential
  backoff, emitting an ``llm_retry`` event per attempt.
- Tool errors are returned to the model as error results so it can recover.
- A max-iteration guard prevents runaway tool loops.
- Any unexpected exception is converted into an apology + ``run_error`` event;
  the stream always terminates with ``run_completed``.
"""

from __future__ import annotations

import asyncio
import random
import time

from ..config import settings
from ..events import EventBus, EventType
from ..llm.base import (
    LLMProvider,
    LLMResponse,
    ProviderNotConfiguredError,
    TransientLLMError,
)
from ..services.crm import CRMService
from ..services.escalations import EscalationService
from ..services.refunds import RefundLedger
from ..services.sessions import ChatSession
from .prompts import build_system_prompt
from .tools import ToolContext, execute_tool, tool_schemas

FALLBACK_LLM_UNAVAILABLE = (
    "I'm sorry — I'm having trouble reaching our systems right now. Please give me a "
    "moment and try again, or ask me to connect you with a human agent."
)
FALLBACK_MAX_ITERATIONS = (
    "I'm sorry — I couldn't complete that within our safety limits. Let me connect you "
    "with a human agent who can take it from here."
)
FALLBACK_REFUSAL = (
    "I'm sorry, I can't help with that request. I'm happy to help with orders, refunds, "
    "returns, or cancellations for your Meridian Goods account."
)
FALLBACK_EMPTY = (
    "Sorry — something went wrong on my end and I didn't get that out properly. "
    "Could you repeat your last message?"
)
# An empty completion (no text, no tool calls) is a provider hiccup, not a
# failure of the conversation — retrying the same context usually succeeds.
MAX_EMPTY_COMPLETION_RETRIES = 1


class AgentRunner:
    def __init__(
        self,
        provider: LLMProvider,
        bus: EventBus,
        crm: CRMService,
        ledger: RefundLedger,
        escalations: EscalationService,
    ) -> None:
        self.provider = provider
        self.bus = bus
        self.crm = crm
        self.ledger = ledger
        self.escalations = escalations
        self.system_prompt = build_system_prompt()
        self.schemas = tool_schemas()

    async def run_turn(self, session: ChatSession, user_text: str) -> str:
        if session.message_count == 0:
            self.bus.emit(
                session.id,
                EventType.SESSION_STARTED,
                f"Session {session.id} started",
                {"provider": self.provider.name, "model": self.provider.model},
            )
        session.message_count += 1
        self.bus.emit(session.id, EventType.USER_MESSAGE, "Customer message", {"text": user_text})
        session.history.append({"role": "user", "content": user_text})

        ctx = ToolContext(
            session=session,
            crm=self.crm,
            ledger=self.ledger,
            escalations=self.escalations,
            bus=self.bus,
        )

        started = time.monotonic()
        usage_totals = {"input_tokens": 0, "output_tokens": 0}
        iterations_used = 0
        empty_completions = 0
        status = "ok"
        final_text: str | None = None

        try:
            for iteration in range(1, settings.max_tool_iterations + 1):
                iterations_used = iteration
                response = await self._complete_with_retry(session, iteration)
                for key in usage_totals:
                    usage_totals[key] += int(response.usage.get(key, 0) or 0)

                if response.stop_reason == "refusal":
                    status = "refusal"
                    final_text = FALLBACK_REFUSAL
                    self.bus.emit(
                        session.id,
                        EventType.RUN_ERROR,
                        "Model declined the request (safety refusal)",
                        {"kind": "refusal"},
                    )
                    break

                if response.tool_calls:
                    if response.text:
                        self.bus.emit(
                            session.id,
                            EventType.ASSISTANT_THINKING,
                            "Agent working note",
                            {"text": response.text},
                        )
                    session.history.append(
                        {
                            "role": "assistant",
                            "content": response.text,
                            "tool_calls": [call.model_dump() for call in response.tool_calls],
                        }
                    )
                    results = []
                    for call in response.tool_calls:
                        results.append(await execute_tool(ctx, call))
                    session.history.append({"role": "tool", "results": results})
                    continue

                if response.text:
                    final_text = response.text
                    break

                # No tool calls and no text: the model returned nothing usable.
                if empty_completions < MAX_EMPTY_COMPLETION_RETRIES:
                    empty_completions += 1
                    self.bus.emit(
                        session.id,
                        EventType.LLM_RETRY,
                        "Model returned an empty reply; retrying once",
                        {
                            "attempt": empty_completions,
                            "max_attempts": MAX_EMPTY_COMPLETION_RETRIES,
                            "reason": "empty_completion",
                            "stop_reason": response.stop_reason,
                        },
                    )
                    continue

                status = "empty_completion"
                final_text = FALLBACK_EMPTY
                self.bus.emit(
                    session.id,
                    EventType.RUN_ERROR,
                    "Model returned an empty reply twice",
                    {"kind": "empty_completion", "stop_reason": response.stop_reason},
                )
                break
            else:
                status = "max_iterations"
                final_text = FALLBACK_MAX_ITERATIONS
                self.bus.emit(
                    session.id,
                    EventType.RUN_ERROR,
                    f"Stopped after {settings.max_tool_iterations} tool iterations (guardrail)",
                    {"kind": "max_iterations", "limit": settings.max_tool_iterations},
                )
        except ProviderNotConfiguredError as exc:
            status = "not_configured"
            final_text = f"⚠️ Backend configuration issue: {exc}"
            self.bus.emit(
                session.id,
                EventType.RUN_ERROR,
                "LLM provider not configured",
                {"kind": "provider_not_configured", "error": str(exc)},
            )
        except TransientLLMError as exc:
            status = "llm_unavailable"
            final_text = FALLBACK_LLM_UNAVAILABLE
            self.bus.emit(
                session.id,
                EventType.RUN_ERROR,
                f"LLM unavailable after {settings.llm_retry_attempts} attempts",
                {"kind": "llm_exhausted", "error": str(exc)},
            )
        # Deliberate catch-all: this is the outermost handler of the request
        # path, and an unexpected bug must degrade to a polite reply, not a 500.
        except Exception as exc:  # noqa: BLE001
            status = "error"
            final_text = FALLBACK_LLM_UNAVAILABLE
            self.bus.emit(
                session.id,
                EventType.RUN_ERROR,
                f"Unexpected error: {exc.__class__.__name__}",
                {"kind": "unexpected", "error": str(exc)},
            )

        final_text = final_text or FALLBACK_LLM_UNAVAILABLE
        session.history.append({"role": "assistant", "content": final_text, "tool_calls": []})
        self.bus.emit(session.id, EventType.AGENT_RESPONSE, "Agent reply", {"text": final_text})
        self.bus.emit(
            session.id,
            EventType.RUN_COMPLETED,
            f"Turn finished ({status})",
            {
                "status": status,
                "iterations": iterations_used,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "usage": usage_totals,
            },
        )
        return final_text

    async def _complete(self, session: ChatSession) -> LLMResponse:
        """One model call, streaming reply text to the chat view as it arrives.

        Deltas are emitted transiently — they exist only for the live view and
        would otherwise bury the reasoning trace in the event history. The
        authoritative reply is still published once, as ``agent_response``.

        A turn may stream text and *then* decide to call a tool (the text was a
        working note, not the answer). The chat view discards streamed text
        when a tool call follows, keyed off the ``llm_request`` that precedes
        every attempt; nothing here needs to know which case it is.
        """
        if not settings.llm_streaming:
            return await self.provider.complete(
                self.system_prompt, session.history, self.schemas
            )

        def on_delta(fragment: str) -> None:
            self.bus.emit(
                session.id,
                EventType.RESPONSE_DELTA,
                "Reply fragment",
                {"text": fragment},
                transient=True,
            )

        return await self.provider.complete_streaming(
            self.system_prompt, session.history, self.schemas, on_delta
        )

    async def _complete_with_retry(self, session: ChatSession, iteration: int) -> LLMResponse:
        attempts = max(1, settings.llm_retry_attempts)
        for attempt in range(1, attempts + 1):
            self.bus.emit(
                session.id,
                EventType.LLM_REQUEST,
                f"LLM call (iteration {iteration}, attempt {attempt})",
                {
                    "iteration": iteration,
                    "attempt": attempt,
                    "provider": self.provider.name,
                    "model": self.provider.model,
                },
            )
            try:
                return await self._complete(session)
            except TransientLLMError as exc:
                if attempt == attempts:
                    raise
                delay = settings.llm_retry_base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
                if exc.retry_after:
                    # Free-tier per-minute windows send Retry-After far beyond
                    # our backoff curve; honor it (bounded so chat can't stall).
                    delay = max(delay, exc.retry_after + 0.5)
                delay = min(delay, 30.0)
                self.bus.emit(
                    session.id,
                    EventType.LLM_RETRY,
                    f"Transient LLM failure (attempt {attempt}/{attempts}); retrying in {delay:.1f}s",
                    {
                        "attempt": attempt,
                        "max_attempts": attempts,
                        "delay_s": round(delay, 2),
                        "retry_after_s": exc.retry_after,
                        "error": str(exc),
                    },
                )
                await asyncio.sleep(delay)
        raise TransientLLMError("retry loop exhausted")
