"""ElevenLabs TTS service and /api/tts proxy endpoint.

No network calls: the ElevenLabs HTTP layer is stubbed so the proxy's
contract (audio out, graceful 503/502 for fallback) is testable offline.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.services.tts import ElevenLabsTTS, TTSError, TTSNotConfiguredError


@pytest.fixture()
def client(monkeypatch):
    """App with TTS deliberately unconfigured.

    The key is blanked so these tests behave identically whether or not the
    developer has a real ELEVENLABS_API_KEY in their .env; tests that need the
    configured path set ``state.tts._api_key`` explicitly.
    """
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_unconfigured_service_reports_not_configured():
    tts = ElevenLabsTTS(api_key="", voice_id="voice-1", model_id="m")
    assert tts.is_configured is False
    with pytest.raises(TTSNotConfiguredError):
        asyncio.run(tts.synthesize("hello"))


def test_missing_voice_id_is_not_configured():
    tts = ElevenLabsTTS(api_key="key", voice_id="", model_id="m")
    assert tts.is_configured is False


def test_configured_service_reports_ready():
    tts = ElevenLabsTTS(api_key="key", voice_id="voice-1", model_id="m")
    assert tts.is_configured is True


def test_tts_endpoint_returns_503_when_unconfigured(client):
    """503 is the signal the frontend uses to fall back to browser speech."""
    response = client.post("/api/tts", json={"text": "hello"})
    assert response.status_code == 503


def test_tts_endpoint_returns_audio_when_configured(client):
    async def fake_synthesize(text):
        assert text == "Your refund has been processed."
        return b"ID3-fake-mp3-bytes"

    client.app.state.tts._api_key = "test-key"
    client.app.state.tts.synthesize = fake_synthesize

    response = client.post("/api/tts", json={"text": "Your refund has been processed."})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"ID3-fake-mp3-bytes"


def test_tts_endpoint_returns_502_on_provider_failure(client):
    async def failing_synthesize(text):
        raise TTSError("ElevenLabs quota or rate limit reached (429).")

    client.app.state.tts._api_key = "test-key"
    client.app.state.tts.synthesize = failing_synthesize

    response = client.post("/api/tts", json={"text": "hello"})
    assert response.status_code == 502
    assert "quota" in response.json()["detail"]


def test_tts_endpoint_rejects_empty_text(client):
    assert client.post("/api/tts", json={"text": ""}).status_code == 422


def test_config_reports_active_voice_engine(client):
    assert client.get("/api/admin/config").json()["tts_engine"] == "browser"
    client.app.state.tts._api_key = "test-key"
    body = client.get("/api/admin/config").json()
    assert body["tts_engine"] == "elevenlabs"
    assert body["tts_voice_id"] == settings.elevenlabs_voice_id
