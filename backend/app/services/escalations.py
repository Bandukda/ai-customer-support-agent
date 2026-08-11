"""Escalation tickets created when policy requires human review (R8, R9, R11)."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

from ..models import EscalationTicket


class EscalationService:
    def __init__(self) -> None:
        self._tickets: list[EscalationTicket] = []
        self._counter = itertools.count(1)

    def create(
        self,
        customer_email: str,
        summary: str,
        order_id: str | None = None,
    ) -> EscalationTicket:
        ticket = EscalationTicket(
            id=f"ESC-{4000 + next(self._counter)}",
            customer_email=customer_email,
            order_id=order_id,
            summary=summary,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._tickets.append(ticket)
        return ticket

    def all_tickets(self) -> list[EscalationTicket]:
        return self._tickets
