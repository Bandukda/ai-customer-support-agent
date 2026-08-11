"""Token streaming: provider contract, loop wiring, and event-bus behaviour.

The reply must be identical whether or not it streamed — streaming changes
only how the text reaches the browser. These tests pin that down, plus the two
properties the rest of the system depends on: deltas never enter the event
history, and a streamed working note is superseded when a tool call follows.
"""

import asyncio

import pytest

from app.agent.loop import AgentRunner
from app.config import settings
from app.events import EventBus, EventType
from app.llm.base import LLMProvider, LLMResponse, ToolCallRequest
from app.llm.mock_provider import MockProvider, _split_for_playback
from app.llm.openai_provider import OpenAIProvider
from app.services.escalations import EscalationService
from app.services.refunds import RefundLedger
from app.services.sessions import ChatSession


class ScriptedProvider(LLMProvider):
    """Non-streaming provider: exercises the base class's default fallback."""

    name = "fake"
    model = "fake-1"

    def __init__(self, script):
        self.script = list(script)

    async def complete(self, system, messages, tools):
        return self.script.pop(0)


class ChunkedProvider(ScriptedProvider):
    """Streaming provider: emits the reply a few characters at a time."""

    async def complete_streaming(self, system, messages, tools, on_delta):
        response = self.script.pop(0)
        for index in range(0, len(response.text or ""), 4):
            on_delta(response.text[index : index + 4])
        return response


def deltas(bus, session_id):
    """Delta text captured from a live subscriber (they never reach history)."""
    return [
        e.data["text"]
        for e in bus.drained
        if e.type == EventType.RESPONSE_DELTA and e.session_id == session_id
    ]


@pytest.fixture()
def recording_bus():
    """An EventBus that also keeps every emitted event, transient included."""
    bus = EventBus()
    bus.drained = []
    original = bus.emit

    def emit(*args, **kwargs):
        event = original(*args, **kwargs)
        bus.drained.append(event)
        return event

    bus.emit = emit
    return bus


def run_with(crm, provider, bus, text="Refund please. sofia.ramirez@example.com"):
    runner = AgentRunner(
        provider=provider,
        bus=bus,
        crm=crm,
        ledger=RefundLedger(crm),
        escalations=EscalationService(),
    )
    session = ChatSession("sess-stream")
    reply = asyncio.run(runner.run_turn(session, text))
    return reply, session


# ── The event bus's transient contract ────────────────────────────────────────

def test_transient_events_reach_subscribers_but_not_history():
    bus = EventBus()
    queue = bus.subscribe()
    bus.emit("s1", EventType.RESPONSE_DELTA, "fragment", {"text": "hi"}, transient=True)
    assert queue.get_nowait().data["text"] == "hi"
    assert bus.history("s1") == []


def test_normal_events_are_still_recorded():
    bus = EventBus()
    bus.emit("s1", EventType.USER_MESSAGE, "msg", {"text": "hi"})
    assert len(bus.history("s1")) == 1


def test_deltas_cannot_evict_the_reasoning_trace():
    """A long streamed reply must not push real events out of the bounded log."""
    bus = EventBus()
    bus.emit("s1", EventType.USER_MESSAGE, "msg", {"text": "hi"})
    for _ in range(6000):
        bus.emit("s1", EventType.RESPONSE_DELTA, "f", {"text": "x"}, transient=True)
    assert [e.type for e in bus.history("s1")] == [EventType.USER_MESSAGE]


# ── Loop wiring ───────────────────────────────────────────────────────────────

def test_streaming_provider_emits_deltas_that_rebuild_the_reply(crm, recording_bus):
    provider = ChunkedProvider([LLMResponse(text="Your refund is approved.")])
    reply, session = run_with(crm, provider, recording_bus)
    captured = deltas(recording_bus, session.id)
    assert len(captured) > 1, "expected the reply to arrive in several fragments"
    assert "".join(captured) == "Your refund is approved."
    assert reply == "Your refund is approved."


def test_non_streaming_provider_still_produces_one_delta(crm, recording_bus):
    """The base-class default keeps the chat view on a single code path."""
    provider = ScriptedProvider([LLMResponse(text="All set.")])
    reply, session = run_with(crm, provider, recording_bus)
    assert deltas(recording_bus, session.id) == ["All set."]
    assert reply == "All set."


def test_streaming_can_be_disabled_by_config(crm, recording_bus, monkeypatch):
    monkeypatch.setattr(settings, "llm_streaming", False)
    provider = ChunkedProvider([LLMResponse(text="All set.")])
    reply, session = run_with(crm, provider, recording_bus)
    assert deltas(recording_bus, session.id) == []
    assert reply == "All set.", "disabling streaming must not change the answer"


def test_agent_response_is_still_authoritative(crm, recording_bus):
    """The committed reply is published once, in full, after the deltas."""
    provider = ChunkedProvider([LLMResponse(text="Refund approved for $25.00.")])
    _, session = run_with(crm, provider, recording_bus)
    responses = [
        e for e in recording_bus.drained
        if e.type == EventType.AGENT_RESPONSE and e.session_id == session.id
    ]
    assert len(responses) == 1
    assert responses[0].data["text"] == "Refund approved for $25.00."


def test_working_note_streams_before_a_tool_call(crm, recording_bus):
    """Text can stream and *then* be superseded by a tool call.

    The chat view discards it; what matters here is that the turn still ends
    on the real reply and the trace records both steps in order.
    """
    provider = ChunkedProvider(
        [
            LLMResponse(
                text="Let me pull up your account.",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="lookup_customer",
                        arguments={"email": "sofia.ramirez@example.com"},
                    )
                ],
                stop_reason="tool_use",
            ),
            LLMResponse(text="Found it."),
        ]
    )
    reply, session = run_with(crm, provider, recording_bus)
    assert reply == "Found it."
    types = [e.type for e in recording_bus.drained if e.session_id == session.id]
    assert types.index(EventType.TOOL_CALL) < types.index(EventType.AGENT_RESPONSE)


# ── Mock provider playback ────────────────────────────────────────────────────

def test_playback_split_preserves_the_text_exactly():
    text = "Hello there —  spaced\nand newlined."
    assert "".join(_split_for_playback(text)) == text


def test_mock_provider_streams_multiple_fragments():
    provider = MockProvider()
    captured = []
    response = asyncio.run(
        provider.complete_streaming("sys", [{"role": "user", "content": "hi"}], [], captured.append)
    )
    assert len(captured) > 1
    assert "".join(captured) == response.text


# ── OpenAI adapter streaming ──────────────────────────────────────────────────

class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _ToolFragment:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage


class _Usage:
    prompt_tokens = 11
    completion_tokens = 7


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


def streaming_provider(chunks, base_url="https://api.deepseek.com"):
    provider = OpenAIProvider(api_key="k", model="m", max_tokens=100, base_url=base_url)

    async def fake_create(**kwargs):
        fake_create.kwargs = kwargs
        return _FakeStream(chunks)

    provider._client.chat.completions.create = fake_create
    provider.fake_create = fake_create
    return provider


def collect(provider):
    captured = []
    response = asyncio.run(provider.complete_streaming("sys", [], [], captured.append))
    return response, captured


def test_openai_stream_accumulates_text_and_usage():
    provider = streaming_provider(
        [
            _Chunk([_Choice(_Delta(content="Your refund "))]),
            _Chunk([_Choice(_Delta(content="is approved."), finish_reason="stop")]),
            _Chunk([], usage=_Usage()),
        ]
    )
    response, captured = collect(provider)
    assert captured == ["Your refund ", "is approved."]
    assert response.text == "Your refund is approved."
    assert response.stop_reason == "end_turn"
    assert response.usage == {"input_tokens": 11, "output_tokens": 7}


def test_openai_stream_reassembles_tool_call_fragments():
    """Name arrives once; the JSON arguments accumulate across many chunks."""
    provider = streaming_provider(
        [
            _Chunk([_Choice(_Delta(tool_calls=[_ToolFragment(0, "c1", "get_order", '{"order')]))]),
            _Chunk([_Choice(_Delta(tool_calls=[_ToolFragment(0, arguments='_id": "ORD-1"}')]))]),
            _Chunk([_Choice(_Delta(), finish_reason="tool_calls")]),
        ]
    )
    response, captured = collect(provider)
    assert captured == []
    assert response.stop_reason == "tool_use"
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert (call.id, call.name) == ("c1", "get_order")
    assert call.arguments == {"order_id": "ORD-1"}


def test_openai_stream_handles_parallel_tool_calls():
    provider = streaming_provider(
        [
            _Chunk([_Choice(_Delta(tool_calls=[_ToolFragment(0, "a", "get_order", "{}")]))]),
            _Chunk([_Choice(_Delta(tool_calls=[_ToolFragment(1, "b", "lookup_customer", "{}")]))]),
            _Chunk([_Choice(_Delta(), finish_reason="tool_calls")]),
        ]
    )
    response, _ = collect(provider)
    assert [c.name for c in response.tool_calls] == ["get_order", "lookup_customer"]


def test_openai_stream_survives_malformed_tool_arguments():
    provider = streaming_provider(
        [
            _Chunk([_Choice(_Delta(tool_calls=[_ToolFragment(0, "c1", "get_order", "{not json")]))]),
            _Chunk([_Choice(_Delta(), finish_reason="tool_calls")]),
        ]
    )
    response, _ = collect(provider)
    assert response.tool_calls[0].arguments == {"_malformed_arguments": "{not json"}


def test_usage_option_is_omitted_for_local_endpoints():
    """Ollama can reject unknown parameters, so we don't send stream_options."""
    provider = streaming_provider(
        [_Chunk([_Choice(_Delta(content="hi"), finish_reason="stop")])],
        base_url="http://localhost:11434/v1",
    )
    collect(provider)
    assert "stream_options" not in provider.fake_create.kwargs
    assert provider.fake_create.kwargs["stream"] is True


def test_usage_option_is_sent_to_known_hosts():
    provider = streaming_provider([_Chunk([_Choice(_Delta(content="hi"), finish_reason="stop")])])
    collect(provider)
    assert provider.fake_create.kwargs["stream_options"] == {"include_usage": True}


def test_gemini_is_excluded_from_streaming():
    """Streaming would drop the thought_signature Gemini requires back."""
    provider = OpenAIProvider(
        api_key="k",
        model="m",
        max_tokens=100,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    assert provider.supports_streaming is False


@pytest.mark.parametrize(
    "base_url",
    ["https://api.deepseek.com", "https://api.groq.com/openai/v1", "", "http://localhost:11434/v1"],
)
def test_other_endpoints_support_streaming(base_url):
    provider = OpenAIProvider(api_key="k", model="m", max_tokens=100, base_url=base_url)
    assert provider.supports_streaming is True


def test_gemini_falls_back_to_the_non_streaming_path():
    """The fallback still reports the reply through on_delta, in one piece."""
    provider = OpenAIProvider(
        api_key="k",
        model="m",
        max_tokens=100,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )

    async def fake_complete(system, messages, tools):
        return LLMResponse(text="Non-streamed reply.")

    provider.complete = fake_complete
    response, captured = collect(provider)
    assert captured == ["Non-streamed reply."]
    assert response.text == "Non-streamed reply."
