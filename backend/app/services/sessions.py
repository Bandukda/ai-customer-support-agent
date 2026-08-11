"""In-memory chat sessions holding provider-agnostic conversation history."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone


class ChatSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # History uses the normalized message format defined in llm/base.py.
        self.history: list[dict] = []
        self.message_count = 0
        self.last_outcome: str = ""
        self.outage_fired = False

    def summary(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "message_count": self.message_count,
            "last_outcome": self.last_outcome,
        }


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}

    def get_or_create(self, session_id: str | None) -> ChatSession:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        new_id = session_id or f"sess-{secrets.token_hex(4)}"
        session = ChatSession(new_id)
        self._sessions[new_id] = session
        return session

    def get(self, session_id: str) -> ChatSession | None:
        return self._sessions.get(session_id)

    def all_sessions(self) -> list[ChatSession]:
        return sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)
