"""FastAPI application: wires services, agent runner, API routes, and the
static frontend into a single server.

Run from the backend/ directory:  uvicorn app.main:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from .agent.loop import AgentRunner
from .api import admin, chat, voice
from .config import DATA_DIR, FRONTEND_DIR, settings
from .events import EventBus
from .llm import create_provider
from .services.crm import CRMService
from .services.escalations import EscalationService
from .services.refunds import RefundLedger
from .services.sessions import SessionStore
from .services.tts import ElevenLabsTTS


class NoCacheStaticFiles(StaticFiles):
    """Static files without browser caching — stale UI assets are worse than
    the negligible re-fetch cost for a local demo app."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="Meridian Goods — AI Customer Support Agent", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    crm = CRMService(DATA_DIR / "customers.json")
    bus = EventBus()
    ledger = RefundLedger(crm)
    escalations = EscalationService()
    provider = create_provider(settings)

    app.state.crm = crm
    app.state.bus = bus
    app.state.ledger = ledger
    app.state.escalations = escalations
    app.state.sessions = SessionStore()
    app.state.tts = ElevenLabsTTS(
        api_key=settings.elevenlabs_api_key,
        voice_id=settings.elevenlabs_voice_id,
        model_id=settings.elevenlabs_model_id,
    )
    app.state.runner = AgentRunner(
        provider=provider, bus=bus, crm=crm, ledger=ledger, escalations=escalations
    )

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "provider": provider.name,
            "model": provider.model,
            "provider_configured": provider.is_configured,
        }

    app.include_router(chat.router, prefix="/api")
    app.include_router(voice.router, prefix="/api")
    app.include_router(admin.router, prefix="/api")

    # The frontend is dependency-free static ES modules served by this same
    # process, so the whole app runs with a single command.
    app.mount("/", NoCacheStaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()
