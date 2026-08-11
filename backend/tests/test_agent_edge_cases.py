"""Agent-loop and tool-dispatch edge cases: malformed model output, hostile
arguments, and state integrity under failure."""

import asyncio
import json

from app.agent.loop import AgentRunner
from app.agent.tools import ToolContext, execute_tool
from app.events import EventBus, EventType
from app.llm.base import LLMProvider, LLMResponse, ToolCallRequest
from app.services.escalations import EscalationService
from app.services.refunds import RefundLedger
from app.services.sessions import ChatSession, SessionStore


class ScriptedProvider(LLMProvider):
    name, model = "fake", "fake-1"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def complete(self, system, messages, tools):
        self.calls += 1
        item = self.script.pop(0) if self.script else LLMResponse(text="done")
        if isinstance(item, Exception):
            raise item
        return item


def make(crm, script):
    bus = EventBus()
    runner = AgentRunner(
        provider=ScriptedProvider(script), bus=bus, crm=crm,
        ledger=RefundLedger(crm), escalations=EscalationService(),
    )
    return runner, bus


def call(name, args, cid="c1"):
    return LLMResponse(
        tool_calls=[ToolCallRequest(id=cid, name=name, arguments=args)], stop_reason="tool_use"
    )


def ctx_for(crm, session=None):
    return ToolContext(
        session=session or ChatSession("s-tool"),
        crm=crm, ledger=RefundLedger(crm),
        escalations=EscalationService(), bus=EventBus(),
    )


def result_of(crm, name, args, ctx=None):
    ctx = ctx or ctx_for(crm)
    out = asyncio.run(execute_tool(ctx, ToolCallRequest(id="c1", name=name, arguments=args)))
    return out, json.loads(out["content"])


# ── Malformed / hostile tool arguments ────────────────────────────────────────

def test_unknown_tool_name_is_recoverable(crm):
    out, body = result_of(crm, "delete_all_refunds", {})
    assert out["is_error"] is True
    assert "Unknown tool" in body["error"]


def test_missing_required_argument_is_recoverable(crm):
    out, body = result_of(crm, "lookup_customer", {})
    assert out["is_error"] is True
    assert "Invalid arguments" in body["error"]


def test_unexpected_extra_argument_is_rejected(crm):
    """extra='forbid' stops the model smuggling in fields like an amount."""
    out, body = result_of(
        crm, "check_refund_eligibility",
        {"customer_email": "sofia.ramirez@example.com", "order_id": "ORD-72001",
         "reason": "changed_mind", "refund_amount": 999999},
    )
    assert out["is_error"] is True
    assert "Invalid arguments" in body["error"]


def test_invalid_enum_value_is_rejected(crm):
    out, _body = result_of(
        crm, "check_refund_eligibility",
        {"customer_email": "sofia.ramirez@example.com", "order_id": "ORD-72001",
         "reason": "because_i_said_so"},
    )
    assert out["is_error"] is True


def test_wrong_type_argument_is_rejected(crm):
    out, _body = result_of(crm, "lookup_customer", {"email": ["not", "a", "string"]})
    assert out["is_error"] is True


def test_unknown_customer_does_not_leak_other_accounts(crm):
    out, body = result_of(crm, "lookup_customer", {"email": "attacker@evil.com"})
    assert out["is_error"] is True
    for leaked in ("sofia", "ORD-7200", "CUST-10"):
        assert leaked.lower() not in body["error"].lower()


def test_cross_account_order_access_blocked_at_tool_layer(crm):
    out, body = result_of(
        crm, "get_order",
        {"customer_email": "sofia.ramirez@example.com", "order_id": "ORD-72002"},
    )
    assert out["is_error"] is True
    assert "not found on the account" in body["error"]


def test_email_and_order_id_normalization(crm):
    """Whitespace/casing from the model must not cause a false 'not found'."""
    out, body = result_of(
        crm, "check_refund_eligibility",
        {"customer_email": "  SOFIA.Ramirez@Example.COM  ", "order_id": " ord-72001 ",
         "reason": "changed_mind"},
    )
    assert out["is_error"] is False
    assert body["outcome"] == "approved"


def test_tool_result_payload_is_json_serializable(crm):
    out, body = result_of(crm, "lookup_customer", {"email": "sofia.ramirez@example.com"})
    assert out["is_error"] is False
    assert json.dumps(body)  # round-trips cleanly for the model and the log


# ── Defense in depth on process_refund ────────────────────────────────────────

def test_process_refund_requires_confirmation_flag(crm):
    out, body = result_of(
        crm, "process_refund",
        {"customer_email": "sofia.ramirez@example.com", "order_id": "ORD-72001",
         "reason": "changed_mind", "customer_confirmed": False},
    )
    assert out["is_error"] is True
    assert "confirmation" in body["error"].lower()


def test_process_refund_on_denied_order_is_blocked_with_citations(crm):
    ctx = ctx_for(crm)
    out, body = result_of(
        crm, "process_refund",
        {"customer_email": "james.okafor@example.com", "order_id": "ORD-72002",
         "reason": "changed_mind", "customer_confirmed": True},
        ctx=ctx,
    )
    assert out["is_error"] is True
    assert "R1" in body["error"]
    assert ctx.ledger.all_records() == []  # nothing written


def test_duplicate_sku_cannot_inflate_a_processed_refund(crm):
    """The engine dedupes, so the ledger pays the line once."""
    ctx = ctx_for(crm)
    out, body = result_of(
        crm, "process_refund",
        {"customer_email": "jack.thompson@example.com", "order_id": "ORD-72010",
         "reason": "changed_mind", "item_ids": ["GLV-030", "GLV-030", "glv-030"],
         "customer_confirmed": True},
        ctx=ctx,
    )
    assert out["is_error"] is False
    assert body["refund"]["amount"] == 25.0
    assert ctx.ledger.total_refunded == 25.0


# ── Loop-level resilience ─────────────────────────────────────────────────────

def test_empty_completion_is_retried_once_then_succeeds(crm):
    """A provider hiccup that returns nothing should be retried, not surfaced."""
    runner, bus = make(crm, [LLMResponse(text=None), LLMResponse(text="Here you go.")])
    reply = asyncio.run(runner.run_turn(ChatSession("s1"), "hello"))
    assert reply == "Here you go."
    retries = [
        e for e in bus.history(session_id="s1")
        if e.type == EventType.LLM_RETRY and e.data.get("reason") == "empty_completion"
    ]
    assert len(retries) == 1
    assert not [e for e in bus.history(session_id="s1") if e.type == EventType.RUN_ERROR]


def test_persistently_empty_completion_reports_honestly(crm):
    """Twice empty: say something true, not 'trouble reaching our systems'."""
    runner, bus = make(crm, [LLMResponse(text=None), LLMResponse(text=None)])
    reply = asyncio.run(runner.run_turn(ChatSession("s1b"), "hello"))
    assert reply.strip()
    assert "reaching our systems" not in reply
    errors = [e for e in bus.history(session_id="s1b") if e.type == EventType.RUN_ERROR]
    assert errors and errors[0].data["kind"] == "empty_completion"
    done = bus.history(session_id="s1b")[-1]
    assert done.type == EventType.RUN_COMPLETED
    assert done.data["status"] == "empty_completion"


def test_safety_refusal_is_handled_gracefully(crm):
    runner, bus = make(crm, [LLMResponse(stop_reason="refusal")])
    reply = asyncio.run(runner.run_turn(ChatSession("s2"), "do something disallowed"))
    assert "can't help" in reply.lower()
    errors = [e for e in bus.history(session_id="s2") if e.type == EventType.RUN_ERROR]
    assert errors[0].data["kind"] == "refusal"


def test_parallel_tool_calls_all_execute_and_pair_up(crm):
    """Every tool_use must get exactly one matching result, or the next
    provider request would be malformed."""
    response = LLMResponse(
        tool_calls=[
            ToolCallRequest(id="a", name="lookup_customer",
                            arguments={"email": "sofia.ramirez@example.com"}),
            ToolCallRequest(id="b", name="get_order",
                            arguments={"customer_email": "sofia.ramirez@example.com",
                                       "order_id": "ORD-72001"}),
        ],
        stop_reason="tool_use",
    )
    runner, _ = make(crm, [response, LLMResponse(text="Both fetched.")])
    session = ChatSession("s3")
    asyncio.run(runner.run_turn(session, "look me up"))
    tool_msg = next(m for m in session.history if m["role"] == "tool")
    assert [r["tool_call_id"] for r in tool_msg["results"]] == ["a", "b"]


def test_one_failing_tool_does_not_abort_its_sibling(crm):
    response = LLMResponse(
        tool_calls=[
            ToolCallRequest(id="a", name="lookup_customer", arguments={"email": "nobody@x.com"}),
            ToolCallRequest(id="b", name="lookup_customer",
                            arguments={"email": "sofia.ramirez@example.com"}),
        ],
        stop_reason="tool_use",
    )
    runner, _ = make(crm, [response, LLMResponse(text="Recovered.")])
    session = ChatSession("s4")
    asyncio.run(runner.run_turn(session, "look up two people"))
    results = next(m for m in session.history if m["role"] == "tool")["results"]
    assert [r["is_error"] for r in results] == [True, False]


def test_history_stays_well_formed_across_multiple_turns(crm):
    runner, _ = make(crm, [
        call("lookup_customer", {"email": "sofia.ramirez@example.com"}),
        LLMResponse(text="Found you."),
        LLMResponse(text="Anything else?"),
    ])
    session = ChatSession("s5")
    asyncio.run(runner.run_turn(session, "hi"))
    asyncio.run(runner.run_turn(session, "thanks"))
    roles = [m["role"] for m in session.history]
    assert roles == ["user", "assistant", "tool", "assistant", "user", "assistant"]
    # Every assistant tool_call must be answered by a tool message.
    for index, message in enumerate(session.history):
        if message["role"] == "assistant" and message.get("tool_calls"):
            assert session.history[index + 1]["role"] == "tool"


def test_run_completed_is_always_last_even_on_failure(crm):
    for script in ([LLMResponse(text="ok")], [LLMResponse(stop_reason="refusal")]):
        runner, bus = make(crm, script)
        session = ChatSession(f"s-{id(script)}")
        asyncio.run(runner.run_turn(session, "hi"))
        events = bus.history(session_id=session.id)
        assert events[-1].type == EventType.RUN_COMPLETED
        assert events[-2].type == EventType.AGENT_RESPONSE


def test_session_store_isolates_sessions():
    store = SessionStore()
    a, b = store.get_or_create(None), store.get_or_create(None)
    assert a.id != b.id
    a.history.append({"role": "user", "content": "secret"})
    assert b.history == []
    assert store.get_or_create(a.id) is a


def test_event_bus_filters_by_session():
    bus = EventBus()
    bus.emit("s-a", EventType.USER_MESSAGE, "a")
    bus.emit("s-b", EventType.USER_MESSAGE, "b")
    assert len(bus.history(session_id="s-a")) == 1
    assert len(bus.history()) == 2


def test_event_bus_is_bounded(crm):
    """The in-memory log must not grow without limit during a long demo."""
    from app.events import MAX_EVENT_HISTORY

    bus = EventBus()
    for i in range(MAX_EVENT_HISTORY + 50):
        bus.emit("s", EventType.USER_MESSAGE, f"m{i}")
    assert len(bus.history(limit=MAX_EVENT_HISTORY + 100)) == MAX_EVENT_HISTORY
