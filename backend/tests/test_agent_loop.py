"""Agent loop behavior with a scripted fake LLM: tool dispatch, event trace,
retry with backoff, error recovery, and the max-iteration guardrail."""

import asyncio

from app.agent.loop import AgentRunner
from app.config import settings
from app.events import EventBus, EventType
from app.llm.base import LLMProvider, LLMResponse, ToolCallRequest, TransientLLMError
from app.services.escalations import EscalationService
from app.services.refunds import RefundLedger
from app.services.sessions import ChatSession


class FakeProvider(LLMProvider):
    name = "fake"
    model = "fake-1"

    def __init__(self, script):
        self.script = list(script)

    async def complete(self, system, messages, tools):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_runner(crm, script):
    bus = EventBus()
    runner = AgentRunner(
        provider=FakeProvider(script),
        bus=bus,
        crm=crm,
        ledger=RefundLedger(crm),
        escalations=EscalationService(),
    )
    return runner, bus


def run(runner, session, text):
    return asyncio.run(runner.run_turn(session, text))


def tool_call(name, arguments, call_id="call-1"):
    return LLMResponse(
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=arguments)],
        stop_reason="tool_use",
    )


def test_tool_dispatch_and_event_trace(crm):
    script = [
        tool_call("lookup_customer", {"email": "sofia.ramirez@example.com"}),
        LLMResponse(text="Found your account, Sofia!"),
    ]
    runner, bus = make_runner(crm, script)
    session = ChatSession("sess-test-1")
    reply = run(runner, session, "Hi, refund please. sofia.ramirez@example.com")

    assert reply == "Found your account, Sofia!"
    types = [e.type for e in bus.history(session_id="sess-test-1")]
    assert types[0] == EventType.SESSION_STARTED
    assert EventType.TOOL_CALL in types
    assert EventType.TOOL_RESULT in types
    assert types[-2] == EventType.AGENT_RESPONSE
    assert types[-1] == EventType.RUN_COMPLETED
    # History: user, assistant(tool call), tool results, final assistant.
    roles = [m["role"] for m in session.history]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_transient_llm_errors_retry_then_succeed(crm, monkeypatch):
    monkeypatch.setattr(settings, "llm_retry_base_delay", 0.01)
    script = [
        TransientLLMError("simulated rate limit"),
        TransientLLMError("simulated 529"),
        LLMResponse(text="Recovered after retries."),
    ]
    runner, bus = make_runner(crm, script)
    session = ChatSession("sess-test-2")
    reply = run(runner, session, "hello")

    assert reply == "Recovered after retries."
    retries = [e for e in bus.history(session_id="sess-test-2") if e.type == EventType.LLM_RETRY]
    assert len(retries) == 2


def test_retry_honors_provider_retry_after(crm, monkeypatch):
    monkeypatch.setattr(settings, "llm_retry_base_delay", 0.01)
    script = [
        TransientLLMError("simulated TPM limit", retry_after=0.05),
        LLMResponse(text="ok"),
    ]
    runner, bus = make_runner(crm, script)
    session = ChatSession("sess-test-ra")
    reply = run(runner, session, "hello")

    assert reply == "ok"
    retry = next(e for e in bus.history(session_id="sess-test-ra") if e.type == EventType.LLM_RETRY)
    assert retry.data["retry_after_s"] == 0.05
    assert retry.data["delay_s"] >= 0.05


def test_llm_exhaustion_produces_graceful_fallback(crm, monkeypatch):
    monkeypatch.setattr(settings, "llm_retry_base_delay", 0.01)
    monkeypatch.setattr(settings, "llm_retry_attempts", 2)
    script = [TransientLLMError("boom"), TransientLLMError("boom")]
    runner, bus = make_runner(crm, script)
    session = ChatSession("sess-test-3")
    reply = run(runner, session, "hello")

    assert "trouble" in reply.lower()
    events = bus.history(session_id="sess-test-3")
    errors = [e for e in events if e.type == EventType.RUN_ERROR]
    assert errors and errors[0].data["kind"] == "llm_exhausted"
    assert events[-1].type == EventType.RUN_COMPLETED


def test_tool_error_returned_to_model_for_recovery(crm):
    script = [
        tool_call("lookup_customer", {"email": "nobody@example.com"}),
        LLMResponse(text="I couldn't find that email — could you double-check it?"),
    ]
    runner, bus = make_runner(crm, script)
    session = ChatSession("sess-test-4")
    reply = run(runner, session, "refund for nobody@example.com")

    assert "double-check" in reply
    events = bus.history(session_id="sess-test-4")
    assert any(e.type == EventType.TOOL_ERROR for e in events)
    tool_msg = next(m for m in session.history if m["role"] == "tool")
    assert tool_msg["results"][0]["is_error"] is True


def test_max_iteration_guardrail(crm, monkeypatch):
    monkeypatch.setattr(settings, "max_tool_iterations", 3)
    script = [
        tool_call("lookup_customer", {"email": "sofia.ramirez@example.com"}, f"call-{i}")
        for i in range(10)
    ]
    runner, bus = make_runner(crm, script)
    session = ChatSession("sess-test-5")
    reply = run(runner, session, "loop forever")

    assert "human agent" in reply
    errors = [e for e in bus.history(session_id="sess-test-5") if e.type == EventType.RUN_ERROR]
    assert errors and errors[0].data["kind"] == "max_iterations"


def test_process_refund_via_tools_updates_ledger(crm):
    script = [
        tool_call(
            "process_refund",
            {
                "customer_email": "sofia.ramirez@example.com",
                "order_id": "ORD-72001",
                "reason": "changed_mind",
                "customer_confirmed": True,
            },
        ),
        LLMResponse(text="Your refund is on its way."),
    ]
    runner, bus = make_runner(crm, script)
    session = ChatSession("sess-test-6")
    reply = run(runner, session, "yes, please process it")

    assert reply == "Your refund is on its way."
    assert runner.ledger.total_refunded == 89.99
    types = [e.type for e in bus.history(session_id="sess-test-6")]
    assert EventType.POLICY_DECISION in types
    assert EventType.REFUND_PROCESSED in types
