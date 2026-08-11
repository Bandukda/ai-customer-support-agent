"""Customer chat endpoint.

POST /api/chat runs one agent turn and streams that turn's reasoning events —
ending with the agent's reply — as Server-Sent Events, so the chat UI can show
live status ("Looking up your account…") while the agent works.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..events import EventType

router = APIRouter()

TURN_TIMEOUT_SECONDS = 300

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)


@router.post("/chat")
async def chat(payload: ChatRequest, request: Request) -> StreamingResponse:
    state = request.app.state
    session = state.sessions.get_or_create(payload.session_id)
    queue = state.bus.subscribe()
    task = asyncio.create_task(state.runner.run_turn(session, payload.message))

    async def stream():
        try:
            yield f"data: {json.dumps({'type': 'session_info', 'session_id': session.id})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=TURN_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'stream_timeout'})}\n\n"
                    break
                if event.session_id != session.id:
                    continue
                yield f"data: {event.model_dump_json()}\n\n"
                if event.type == EventType.RUN_COMPLETED:
                    break
            await task
        finally:
            state.bus.unsubscribe(queue)
            if not task.done():
                task.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)
