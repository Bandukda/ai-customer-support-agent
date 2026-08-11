"""Voice endpoints: server-side text-to-speech proxy.

Keeping synthesis behind our own endpoint means the ElevenLabs key never
reaches the browser. A 503 here is a normal, expected condition — the client
falls back to the browser's built-in speech synthesis.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..services.tts import TTSError, TTSNotConfiguredError

router = APIRouter()


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/tts")
async def synthesize_speech(payload: TTSRequest, request: Request) -> Response:
    tts = request.app.state.tts
    try:
        audio = await tts.synthesize(payload.text)
    except TTSNotConfiguredError as exc:
        # 503 => client falls back to browser speech synthesis.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TTSError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/voices")
async def list_voices(request: Request) -> dict:
    """List voices on the configured ElevenLabs account (to pick a voice ID)."""
    tts = request.app.state.tts
    try:
        return {"voices": await tts.list_voices(), "active_voice_id": tts.voice_id}
    except TTSNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TTSError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
