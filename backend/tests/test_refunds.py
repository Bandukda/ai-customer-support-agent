"""Refund ledger: defense-in-depth revalidation, confirmation gate, idempotency."""

import pytest

from app.services.refunds import (
    ConfirmationRequiredError,
    RefundBlockedError,
    RefundLedger,
)


def test_process_approved_refund(crm):
    ledger = RefundLedger(crm)
    record, decision = ledger.process_refund(
        customer_email="sofia.ramirez@example.com",
        order_id="ORD-72001",
        reason="changed_mind",
        customer_confirmed=True,
    )
    assert record.amount == 89.99
    assert decision.outcome == "approved"
    assert ledger.total_refunded == 89.99
    _, order = crm.find_customer("sofia.ramirez@example.com"), crm.get_order("ORD-72001")[1]
    assert any(r.id == record.id for r in order.refunds)


def test_double_refund_blocked_by_r7(crm):
    ledger = RefundLedger(crm)
    ledger.process_refund(
        customer_email="sofia.ramirez@example.com",
        order_id="ORD-72001",
        reason="changed_mind",
        customer_confirmed=True,
    )
    with pytest.raises(RefundBlockedError) as excinfo:
        ledger.process_refund(
            customer_email="sofia.ramirez@example.com",
            order_id="ORD-72001",
            reason="changed_mind",
            customer_confirmed=True,
        )
    assert any(c.rule_id == "R7" for c in excinfo.value.decision.rule_citations)


def test_confirmation_required(crm):
    ledger = RefundLedger(crm)
    with pytest.raises(ConfirmationRequiredError):
        ledger.process_refund(
            customer_email="sofia.ramirez@example.com",
            order_id="ORD-72001",
            reason="changed_mind",
            customer_confirmed=False,
        )


def test_ineligible_refund_blocked(crm):
    ledger = RefundLedger(crm)
    with pytest.raises(RefundBlockedError) as excinfo:
        ledger.process_refund(
            customer_email="james.okafor@example.com",
            order_id="ORD-72002",
            reason="changed_mind",
            customer_confirmed=True,
        )
    assert excinfo.value.decision.outcome == "denied"
    assert ledger.all_records() == []


def test_wrong_owner_rejected(crm):
    ledger = RefundLedger(crm)
    with pytest.raises(LookupError):
        ledger.process_refund(
            customer_email="sofia.ramirez@example.com",
            order_id="ORD-72002",  # belongs to James
            reason="changed_mind",
            customer_confirmed=True,
        )
