"""Unit tests for the deterministic policy engine — one scenario per rule."""

import pytest

from app.services.policy import UnknownItemError, evaluate_refund_eligibility
from tests.conftest import get_customer_order


def check(crm, email, order_id, reason, **kwargs):
    customer, order = get_customer_order(crm, email, order_id)
    return evaluate_refund_eligibility(customer, order, reason, today=crm.today, **kwargs)


def test_r1_within_window_approved(crm):
    decision = check(crm, "sofia.ramirez@example.com", "ORD-72001", "changed_mind")
    assert decision.outcome == "approved"
    assert decision.refund_amount == 89.99
    assert decision.includes_shipping is False
    assert decision.requires_customer_confirmation is True
    assert any(c.rule_id == "R1" for c in decision.rule_citations)


def test_r1_outside_window_denied(crm):
    decision = check(crm, "james.okafor@example.com", "ORD-72002", "changed_mind")
    assert decision.outcome == "denied"
    assert decision.refund_amount == 0.0
    assert decision.rule_citations[0].rule_id == "R1"


def test_r2_in_transit_denied(crm):
    decision = check(crm, "lena.petrov@example.com", "ORD-72005", "changed_mind")
    assert decision.outcome == "denied"
    assert decision.rule_citations[0].rule_id == "R2"


def test_r3_damage_within_seven_days_full_refund_incl_shipping(crm):
    decision = check(crm, "henry.silva@example.com", "ORD-72008", "defective_or_damaged")
    assert decision.outcome == "approved"
    assert decision.includes_shipping is True
    assert decision.refund_amount == round(58.0 + 6.5, 2)


def test_r3_damage_reported_late_denied(crm):
    # Sofia's order was delivered 12 days ago — outside the 7-day R3 window.
    decision = check(crm, "sofia.ramirez@example.com", "ORD-72001", "defective_or_damaged")
    assert decision.outcome == "denied"
    assert decision.rule_citations[0].rule_id == "R3"


def test_r4_final_sale_denied(crm):
    decision = check(crm, "mia.chen@example.com", "ORD-72003", "changed_mind")
    assert decision.outcome == "denied"
    assert any(c.rule_id == "R4" for c in decision.rule_citations)


def test_r4_perishable_denied(crm):
    decision = check(crm, "liam.gallagher@example.com", "ORD-72012", "changed_mind")
    assert decision.outcome == "denied"
    assert any(c.rule_id == "R4" for c in decision.rule_citations)


def test_r5_gift_card_denied(crm):
    decision = check(crm, "ethan.brooks@example.com", "ORD-72004", "changed_mind")
    assert decision.outcome == "denied"
    assert any(c.rule_id == "R5" for c in decision.rule_citations)


def test_r6_opened_electronics_restocking_fee(crm):
    decision = check(
        crm, "kim.nguyen@example.com", "ORD-72011", "changed_mind", item_condition="opened"
    )
    assert decision.outcome == "approved"
    assert decision.refund_amount == round(129.0 * 0.85, 2)
    assert any(c.rule_id == "R6" for c in decision.rule_citations)


def test_r6_used_electronics_denied(crm):
    decision = check(
        crm, "kim.nguyen@example.com", "ORD-72011", "changed_mind", item_condition="used"
    )
    assert decision.outcome == "denied"
    assert any(c.rule_id == "R6" for c in decision.rule_citations)


def test_r7_already_refunded_denied(crm):
    decision = check(crm, "grace.nwosu@example.com", "ORD-72007", "changed_mind")
    assert decision.outcome == "denied"
    assert any(c.rule_id == "R7" for c in decision.rule_citations)


def test_r8_above_auto_approval_limit_needs_review(crm):
    decision = check(crm, "david.kim@example.com", "ORD-72006", "changed_mind")
    assert decision.outcome == "needs_human_review"
    assert decision.refund_amount == 649.0
    assert any(c.rule_id == "R8" for c in decision.rule_citations)
    assert decision.requires_customer_confirmation is False


def test_r9_flagged_account_needs_review(crm):
    decision = check(crm, "isabella.rossi@example.com", "ORD-72009", "changed_mind")
    assert decision.outcome == "needs_human_review"
    assert any(c.rule_id == "R9" for c in decision.rule_citations)


def test_r10_pre_shipment_cancellation_full_refund(crm):
    decision = check(crm, "maya.patel@example.com", "ORD-72013", "order_cancellation")
    assert decision.outcome == "approved"
    assert decision.includes_shipping is True
    assert decision.refund_amount == round(289.0 + 12.99, 2)
    assert any(c.rule_id == "R10" for c in decision.rule_citations)


def test_r11_item_not_received_needs_review(crm):
    decision = check(crm, "noah.fischer@example.com", "ORD-72014", "item_not_received")
    assert decision.outcome == "needs_human_review"
    assert any(c.rule_id == "R11" for c in decision.rule_citations)


def test_partial_refund_single_line(crm):
    decision = check(
        crm, "jack.thompson@example.com", "ORD-72010", "changed_mind", item_ids=["GLV-030"]
    )
    assert decision.outcome == "approved"
    assert decision.refund_amount == 25.0
    assert len(decision.item_assessments) == 1


def test_unknown_sku_raises(crm):
    customer, order = get_customer_order(crm, "jack.thompson@example.com", "ORD-72010")
    with pytest.raises(UnknownItemError):
        evaluate_refund_eligibility(
            customer, order, "changed_mind", item_ids=["NOPE-1"], today=crm.today
        )
