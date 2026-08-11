"""Admin dashboard API: live reasoning-log stream, stats, and ledgers."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..config import DATA_DIR, settings
from ..events import EventType

router = APIRouter(prefix="/admin")

HEARTBEAT_SECONDS = 25

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.get("/stream")
async def admin_stream(request: Request) -> StreamingResponse:
    """Replay recent events, then stream all sessions' events live."""
    bus = request.app.state.bus
    queue = bus.subscribe()

    async def stream():
        try:
            for event in bus.history(limit=300):
                yield f"data: {event.model_dump_json()}\n\n"
            yield f"data: {json.dumps({'type': 'replay_complete'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                # Token deltas are a chat-view concern; a single reply produces
                # hundreds and they carry no reasoning the dashboard can audit.
                if event.type == EventType.RESPONSE_DELTA:
                    continue
                yield f"data: {event.model_dump_json()}\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/events")
async def list_events(request: Request, session_id: str | None = None, limit: int = 500) -> dict:
    events = request.app.state.bus.history(session_id=session_id, limit=min(limit, 2000))
    return {"events": [event.model_dump() for event in events]}


@router.get("/sessions")
async def list_sessions(request: Request) -> dict:
    return {"sessions": [s.summary() for s in request.app.state.sessions.all_sessions()]}


@router.get("/stats")
async def stats(request: Request) -> dict:
    state = request.app.state
    events = state.bus.history(limit=5000)
    denials = sum(
        1
        for e in events
        if e.type == EventType.POLICY_DECISION
        and e.data.get("phase") == "eligibility_check"
        and e.data.get("decision", {}).get("outcome") == "denied"
    )
    tokens = {"input_tokens": 0, "output_tokens": 0}
    for e in events:
        if e.type == EventType.RUN_COMPLETED:
            for key in tokens:
                tokens[key] += int(e.data.get("usage", {}).get(key, 0) or 0)
    refunds = state.ledger.all_records()
    return {
        "sessions": len(state.sessions.all_sessions()),
        "refunds_processed": len(refunds),
        "refunds_total_amount": state.ledger.total_refunded,
        "denials": denials,
        "escalations": len(state.escalations.all_tickets()),
        "errors": sum(1 for e in events if e.type == EventType.RUN_ERROR),
        "retries": sum(1 for e in events if e.type in (EventType.LLM_RETRY, EventType.TOOL_RETRY)),
        "tokens": tokens,
    }


@router.get("/refunds")
async def list_refunds(request: Request) -> dict:
    return {"refunds": [r.model_dump() for r in request.app.state.ledger.all_records()]}


@router.get("/escalations")
async def list_escalations(request: Request) -> dict:
    return {"tickets": [t.model_dump() for t in request.app.state.escalations.all_tickets()]}


@router.get("/customers")
async def list_customers(request: Request) -> dict:
    customers = []
    for customer in request.app.state.crm.all_customers():
        customers.append(
            {
                "id": customer.id,
                "name": customer.name,
                "email": customer.email,
                "tier": customer.tier,
                "flags": customer.flags,
                "demo_notes": customer.demo_notes,
                "orders": [
                    {
                        "id": order.id,
                        "status": order.status,
                        "placed_at": order.placed_at,
                        "delivered_at": order.delivered_at,
                        "items_total": order.items_total,
                        "shipping_fee": order.shipping_fee,
                        "items": [
                            {"sku": i.sku, "name": i.name, "quantity": i.quantity, "unit_price": i.unit_price}
                            for i in order.items
                        ],
                        "refund_count": len(order.refunds),
                    }
                    for order in customer.orders
                ],
            }
        )
    return {"customers": customers}


@router.get("/policy")
async def policy_document() -> dict:
    return {"markdown": (DATA_DIR / "refund_policy.md").read_text(encoding="utf-8")}


@router.get("/config")
async def config(request: Request) -> dict:
    provider = request.app.state.runner.provider
    tts = request.app.state.tts
    return {
        "provider": provider.name,
        "provider_label": provider.display_name,
        "model": provider.model,
        "base_url": getattr(provider, "base_url", "") or None,
        "configured": provider.is_configured,
        "tts_engine": "elevenlabs" if tts.is_configured else "browser",
        "tts_voice_id": tts.voice_id if tts.is_configured else None,
        "effort": settings.llm_effort if provider.name == "anthropic" else None,
        "simulate_crm_outage": settings.demo_simulate_crm_outage,
        "max_tool_iterations": settings.max_tool_iterations,
    }
