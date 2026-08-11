"""Refund execution with defense-in-depth.

``process_refund`` re-runs the policy engine at execution time, so even if the
LLM tried to process an ineligible refund (or was manipulated into trying), the
ledger refuses. The refund amount is always computed by the engine — it is never
accepted from the model.
"""

from __future__ import annotations

import itertools
from datetime import date

from ..models import ItemCondition, PolicyDecision, RefundReason, RefundRecord
from .crm import CRMService
from .policy import evaluate_refund_eligibility


class RefundBlockedError(Exception):
    def __init__(self, decision: PolicyDecision) -> None:
        super().__init__(decision.summary)
        self.decision = decision


class ConfirmationRequiredError(Exception):
    pass


class RefundLedger:
    def __init__(self, crm: CRMService) -> None:
        self._crm = crm
        self._records: list[RefundRecord] = []
        self._counter = itertools.count(1)

    def process_refund(
        self,
        customer_email: str,
        order_id: str,
        reason: RefundReason,
        item_ids: list[str] | None = None,
        item_condition: ItemCondition = "unopened",
        customer_confirmed: bool = False,
        today: date | None = None,
    ) -> tuple[RefundRecord, PolicyDecision]:
        today = today or self._crm.today
        customer = self._crm.find_customer(customer_email)
        if customer is None:
            raise LookupError(f"No customer found for email '{customer_email}'.")
        match = self._crm.get_order(order_id)
        if match is None or match[0].id != customer.id:
            raise LookupError(f"Order '{order_id}' was not found on this customer's account.")
        _, order = match

        if not customer_confirmed:
            raise ConfirmationRequiredError(
                "The customer's explicit confirmation is required before processing (R12). "
                "Ask the customer to confirm, then retry with customer_confirmed=true."
            )

        decision = evaluate_refund_eligibility(
            customer, order, reason, item_ids, item_condition, today
        )
        if decision.outcome != "approved":
            raise RefundBlockedError(decision)

        record = RefundRecord(
            id=f"RF-{7000 + next(self._counter)}",
            order_id=order.id,
            customer_id=customer.id,
            item_skus=[a.sku for a in decision.item_assessments if a.eligible],
            amount=decision.refund_amount,
            reason=reason,
            method=decision.refund_method or "original_payment",
            status="processed",
            processed_at=today.isoformat(),
        )
        self._crm.add_refund(order, record)
        self._records.append(record)
        return record, decision

    def all_records(self) -> list[RefundRecord]:
        return self._records

    @property
    def total_refunded(self) -> float:
        return round(sum(r.amount for r in self._records), 2)
