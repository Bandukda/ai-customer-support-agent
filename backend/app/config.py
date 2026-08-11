"""Application settings, loaded from environment variables and the project .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(__file__).resolve().parent / "data"
FRONTEND_DIR = PROJECT_ROOT / "frontend"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM provider: "anthropic" (default), "openai", or "mock" (offline development only)
    llm_provider: str = "anthropic"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    # Effort applies to Claude 4.6+ models: low | medium | high. "low" keeps chat
    # latency snappy for live demos while remaining strong on tool selection.
    llm_effort: str = "low"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    # Point the "openai" provider at any OpenAI-compatible endpoint for $0 usage:
    # Groq, Google Gemini's compat endpoint, or a local Ollama server.
    openai_base_url: str = ""

    # max_tokens caps thinking + response text on Claude Opus 5, so keep headroom.
    llm_max_tokens: int = 16000
    llm_retry_attempts: int = 3
    llm_retry_base_delay: float = 1.0

    # Stream the reply token-by-token to the chat UI. Providers that don't
    # implement streaming fall back to delivering the reply in one piece, so
    # turning this off only changes how the text arrives, never the outcome.
    llm_streaming: bool = True

    max_tool_iterations: int = 8

    # ── Voice (text-to-speech) ────────────────────────────────────────────────
    # When ELEVENLABS_API_KEY is set, agent replies are spoken with ElevenLabs;
    # otherwise the frontend falls back to the browser's speech synthesis.
    elevenlabs_api_key: str = ""
    # "Bella — professional, bright, warm": suits a support agent.
    # Voice availability varies by account; call GET /api/voices to list yours.
    elevenlabs_voice_id: str = "hpp4J3VqNfWAUOO0d1Us"
    # ElevenLabs' documented default model; lower-latency families exist —
    # see https://elevenlabs.io/docs/models
    elevenlabs_model_id: str = "eleven_multilingual_v2"

    # When true, the first lookup_customer call in each session fails with a
    # simulated CRM timeout so the tool-retry path can be demonstrated live.
    demo_simulate_crm_outage: bool = False


settings = Settings()
