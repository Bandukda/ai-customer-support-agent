"""ElevenLabs text-to-speech.

The API key stays server-side: the browser posts text to our own /api/tts
endpoint, which proxies to ElevenLabs and returns audio. The key is never
exposed to the client, and the frontend falls back to the browser's built-in
speech synthesis whenever this service is unconfigured or failing — so voice
never breaks the demo.
"""

from __future__ import annotations

import httpx

API_ROOT = "https://api.elevenlabs.io/v1"
REQUEST_TIMEOUT_SECONDS = 30
# Support replies are short; this guards against burning free-tier quota on a
# runaway input and keeps latency predictable.
MAX_CHARS = 1200


class TTSNotConfiguredError(Exception):
    """No ElevenLabs API key configured."""


class TTSError(Exception):
    """ElevenLabs rejected the request or was unreachable."""


class ElevenLabsTTS:
    def __init__(self, api_key: str, voice_id: str, model_id: str) -> None:
        self._api_key = api_key
        self.voice_id = voice_id
        self.model_id = model_id
        self.chars_used = 0

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self.voice_id)

    @property
    def _headers(self) -> dict:
        return {"xi-api-key": self._api_key, "Content-Type": "application/json"}

    async def synthesize(self, text: str) -> bytes:
        """Convert text to MP3 audio. Raises TTSNotConfiguredError / TTSError."""
        if not self.is_configured:
            raise TTSNotConfiguredError(
                "ELEVENLABS_API_KEY (and ELEVENLABS_VOICE_ID) are not set."
            )
        clean = text.strip()[:MAX_CHARS]
        if not clean:
            raise TTSError("No text to synthesize.")

        payload = {
            "text": clean,
            "model_id": self.model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{API_ROOT}/text-to-speech/{self.voice_id}",
                    headers=self._headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise TTSError(f"Could not reach ElevenLabs: {exc}") from exc

        if response.status_code != 200:
            raise TTSError(self._describe_failure(response))

        self.chars_used += len(clean)
        return response.content

    async def list_voices(self) -> list[dict]:
        """Voices available on the configured account (helper for picking an ID)."""
        if not self._api_key:
            raise TTSNotConfiguredError("ELEVENLABS_API_KEY is not set.")
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.get(f"{API_ROOT}/voices", headers=self._headers)
        except httpx.HTTPError as exc:
            raise TTSError(f"Could not reach ElevenLabs: {exc}") from exc
        if response.status_code != 200:
            raise TTSError(self._describe_failure(response))
        return [
            {"voice_id": voice.get("voice_id"), "name": voice.get("name")}
            for voice in response.json().get("voices", [])
        ]

    def _describe_failure(self, response: httpx.Response) -> str:
        detail = response.text[:300]
        if response.status_code == 401:
            return "ElevenLabs rejected the API key (401). Check ELEVENLABS_API_KEY."
        if response.status_code == 404:
            return (
                f"Voice '{self.voice_id}' was not found (404). "
                "List the voices on your account at GET /api/voices and set ELEVENLABS_VOICE_ID."
            )
        if response.status_code == 422:
            return f"ElevenLabs rejected the request (422) — check ELEVENLABS_MODEL_ID. {detail}"
        if response.status_code == 429:
            return "ElevenLabs quota or rate limit reached (429)."
        return f"ElevenLabs error {response.status_code}: {detail}"
