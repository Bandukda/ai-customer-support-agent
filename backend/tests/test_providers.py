"""Provider configuration: OpenAI-compatible base_url support (Groq/Gemini/Ollama)."""

from app.config import settings
from app.llm import create_provider
from app.llm.openai_provider import OpenAIProvider


def test_cloud_compat_endpoint_requires_key():
    provider = OpenAIProvider(
        api_key="", model="llama-3.3-70b-versatile", max_tokens=100,
        base_url="https://api.groq.com/openai/v1",
    )
    assert provider.is_configured is False


def test_local_endpoint_needs_no_key():
    provider = OpenAIProvider(
        api_key="", model="qwen2.5:7b", max_tokens=100,
        base_url="http://localhost:11434/v1",
    )
    assert provider.is_configured is True


def test_key_alone_is_configured():
    provider = OpenAIProvider(api_key="sk-test", model="gpt-4o", max_tokens=100)
    assert provider.is_configured is True
    assert provider.base_url == ""


def test_factory_passes_base_url(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "gsk-test")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.groq.com/openai/v1")
    provider = create_provider(settings)
    assert isinstance(provider, OpenAIProvider)
    assert provider.base_url == "https://api.groq.com/openai/v1"


def test_tool_schemas_are_provider_portable():
    """Gemini's function-calling schema subset rejects anyOf/$ref; pydantic
    emits anyOf for Optional[...] fields, so schemas are simplified before
    they are sent. Titles are stripped as pure prompt overhead."""
    from app.agent.tools import tool_schemas

    banned = {"anyOf", "allOf", "oneOf", "$ref", "$defs", "title"}
    found: list[str] = []

    def scan(node, path):
        if isinstance(node, dict):
            for key in node:
                if key in banned:
                    found.append(f"{path}.{key}")
            for key, value in node.items():
                scan(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                scan(value, f"{path}[{i}]")

    for spec in tool_schemas():
        scan(spec["input_schema"], spec["name"])
    assert not found, f"non-portable schema constructs: {found}"


def test_optional_fields_survive_schema_simplification():
    """Simplifying the wire schema must not change what the server accepts."""
    from app.agent.tools import CheckEligibilityInput, tool_schemas

    schema = next(
        s["input_schema"] for s in tool_schemas() if s["name"] == "check_refund_eligibility"
    )
    # Optional stays optional...
    assert "item_ids" not in schema["required"]
    assert schema["properties"]["item_ids"]["type"] == "array"
    # ...and validation still accepts both omission and a value.
    assert CheckEligibilityInput(
        customer_email="a@b.com", order_id="ORD-1", reason="changed_mind"
    ).item_ids is None
    assert CheckEligibilityInput(
        customer_email="a@b.com", order_id="ORD-1", reason="changed_mind", item_ids=["X"]
    ).item_ids == ["X"]


class _FakeResponse:
    def __init__(self, headers=None):
        self.headers = headers or {}


class _FakeRateLimit(Exception):
    def __init__(self, message, headers=None):
        super().__init__(message)
        self.response = _FakeResponse(headers)


def test_retry_after_prefers_header():
    from app.llm.openai_provider import _retry_after_seconds

    exc = _FakeRateLimit("rate limited", {"retry-after": "22"})
    assert _retry_after_seconds(exc) == 22.0


def test_retry_after_parsed_from_message_when_no_header():
    """Gemini sends no Retry-After header; the delay is in the message body."""
    from app.llm.openai_provider import _retry_after_seconds

    exc = _FakeRateLimit(
        "Quota exceeded for metric: generate_content_free_tier_requests, "
        "limit: 20, model: gemini-3.6-flash Please retry in 32.809"
    )
    assert _retry_after_seconds(exc) == 32.809


def test_retry_after_none_when_unavailable():
    from app.llm.openai_provider import _retry_after_seconds

    assert _retry_after_seconds(_FakeRateLimit("something went wrong")) is None


def test_rate_limit_detail_names_the_quota():
    """The provider's message identifies which limit was hit — keep it."""
    from app.llm.openai_provider import _rate_limit_detail

    detail = _rate_limit_detail(
        _FakeRateLimit("Error code: 429 - Quota exceeded for metric: "
                       "generate_content_free_tier_requests, limit: 20")
    )
    assert "generate_content_free_tier_requests" in detail
    assert "limit: 20" in detail


def test_display_name_identifies_the_vendor_not_the_adapter():
    """The pill must not say "openai" when requests go to DeepSeek."""
    from app.llm.openai_provider import OpenAIProvider

    cases = {
        "https://api.deepseek.com": "DeepSeek",
        "https://api.groq.com/openai/v1": "Groq",
        "https://generativelanguage.googleapis.com/v1beta/openai": "Gemini",
        "http://localhost:11434/v1": "Ollama (local)",
        "https://api.openai.com/v1": "OpenAI",
        "": "OpenAI",
    }
    for base_url, expected in cases.items():
        provider = OpenAIProvider(api_key="k", model="m", max_tokens=100, base_url=base_url)
        assert provider.display_name == expected, base_url


def test_display_name_falls_back_to_host_for_unknown_endpoints():
    from app.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider(
        api_key="k", model="m", max_tokens=100, base_url="https://llm.acme.internal/v1"
    )
    assert provider.display_name == "llm.acme.internal"


def test_config_endpoint_exposes_provider_label(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "k")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.deepseek.com")
    from app.main import create_app

    with TestClient(create_app()) as client:
        body = client.get("/api/admin/config").json()
    assert body["provider_label"] == "DeepSeek"
    assert body["provider"] == "openai"  # adapter name still reported
