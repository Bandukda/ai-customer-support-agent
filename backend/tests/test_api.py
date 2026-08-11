"""End-to-end API tests using the offline mock provider."""

import json

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def read_sse_events(response):
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["provider"] == "mock"


def test_chat_turn_streams_reasoning_events(client):
    with client.stream(
        "POST",
        "/api/chat",
        json={"message": "I'd like a refund. Email sofia.ramirez@example.com, order ORD-72001."},
    ) as response:
        assert response.status_code == 200
        events = read_sse_events(response)

    types = [e.get("type") for e in events]
    assert types[0] == "session_info"
    assert "tool_call" in types
    assert "tool_result" in types
    assert "policy_decision" in types
    assert "agent_response" in types
    assert types[-1] == "run_completed"


def test_admin_endpoints(client):
    with client.stream(
        "POST",
        "/api/chat",
        json={"message": "Refund please — sofia.ramirez@example.com, ORD-72001."},
    ) as response:
        read_sse_events(response)

    stats = client.get("/api/admin/stats").json()
    assert stats["sessions"] == 1

    customers = client.get("/api/admin/customers").json()["customers"]
    assert len(customers) == 15

    events = client.get("/api/admin/events").json()["events"]
    assert any(e["type"] == "policy_decision" for e in events)

    policy = client.get("/api/admin/policy").json()["markdown"]
    assert "R1" in policy

    config = client.get("/api/admin/config").json()
    assert config["provider"] == "mock"
