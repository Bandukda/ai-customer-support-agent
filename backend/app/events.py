"""Reasoning-log event model and in-process pub/sub bus.

Every step the agent takes — LLM calls, retries, tool calls, policy decisions,
failures — is emitted as an AgentEvent. Events are kept in a bounded in-memory
log (for replay/REST queries) and fanned out to live SSE subscribers (the admin
dashboard and the per-turn chat stream).
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from datetime import datetime, timezone

from pydantic import BaseModel, Field

MAX_EVENT_HISTORY = 5000


class EventType:
    SESSION_STARTED = "session_started"
    USER_MESSAGE = "user_message"
    LLM_REQUEST = "llm_request"
    LLM_RETRY = "llm_retry"
    # One fragment of the reply as it is generated. Emitted transiently: these
    # are for the live chat view only and are never kept in the event history.
    RESPONSE_DELTA = "response_delta"
    ASSISTANT_THINKING = "assistant_thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_RETRY = "tool_retry"
    TOOL_ERROR = "tool_error"
    POLICY_DECISION = "policy_decision"
    REFUND_PROCESSED = "refund_processed"
    ESCALATION_CREATED = "escalation_created"
    AGENT_RESPONSE = "agent_response"
    RUN_ERROR = "run_error"
    RUN_COMPLETED = "run_completed"


class AgentEvent(BaseModel):
    id: int
    session_id: str
    ts: str
    type: str
    label: str
    data: dict = Field(default_factory=dict)


class EventBus:
    def __init__(self) -> None:
        self._events: deque[AgentEvent] = deque(maxlen=MAX_EVENT_HISTORY)
        self._ids = itertools.count(1)
        self._subscribers: set[asyncio.Queue] = set()

    def emit(
        self,
        session_id: str,
        type_: str,
        label: str,
        data: dict | None = None,
        *,
        transient: bool = False,
    ) -> AgentEvent:
        """Publish an event to live subscribers and (unless transient) the log.

        ``transient`` exists for token deltas: a single reply produces hundreds
        of them, which would evict the real reasoning trace from the bounded
        history and swamp the admin dashboard's replay. They are still fanned
        out live, because the chat view consumes them as they arrive.
        """
        event = AgentEvent(
            id=next(self._ids),
            session_id=session_id,
            ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            type=type_,
            label=label,
            data=data or {},
        )
        if not transient:
            self._events.append(event)
        for queue in list(self._subscribers):
            queue.put_nowait(event)
        return event

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def history(self, session_id: str | None = None, limit: int = 500) -> list[AgentEvent]:
        events = list(self._events)
        if session_id:
            events = [e for e in events if e.session_id == session_id]
        return events[-limit:]

    def count_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self._events:
            counts[event.type] = counts.get(event.type, 0) + 1
        return counts
