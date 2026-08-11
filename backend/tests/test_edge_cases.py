"""Edge cases and boundary conditions.

The scenario tests cover the happy paths for each rule; these lock down the
exact boundaries (is day 30 in or out? is $400.00 auto-approved?), input
normalization, and the security properties the policy engine is relied on for.
"""

from datetime import date, timedelta

import pytest

from app.models import Customer, Order, OrderItem, RefundRecord
from app.services.policy import (
    AUTO_APPROVAL_LIMIT,
    DAMAGE_REPORT_WINDOW_DAYS,
    RETURN_WINDOW_DAYS,
    UnknownItemError,
    evaluate_refund_eligibility,
)

TODAY = date(2026, 6, 15)


def build(
    *,
    delivered_days_ago=5,
    status="delivered",
    items=None,
    shipping_fee=5.0,
    flags=None,
    refunds=None,
):
    items = items or [
        OrderItem(sku="SKU-1", name="Widget", category="home_goods", quantity=1, unit_price=50.0)
    ]
    delivered = (
        (TODAY - timedelta(days=delivered_days_ago)).isoformat()
        if delivered_days_ago is not None
        else None
    )
    order = Order(
        id="ORD-1",
        customer_id="CUST-1",
        placed_at=(TODAY - timedelta(days=(delivered_days_ago or 0) + 3)).isoformat(),
        delivered_at=delivered,
        status=status,
        payment_method="Visa ending 1111",
        shipping_fee=shipping_fee,
        items=items,
        refunds=refunds or [],
    )
    customer = Customer(
        id="CUST-1",
        name="Test User",
        email="test@example.com",
        phone="+1-555-0000",
        tier="standard",
        joined_at="2025-01-01",
        flags=flags or [],
        orders=[order],
    )
    return customer, order


def evaluate(customer, order, reason="changed_mind", **kwargs):
    return evaluate_refund_eligibility(customer, order, reason, today=TODAY, **kwargs)


# ── R1: return-window boundary ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "days_ago,expected",
    [
        (RETURN_WINDOW_DAYS - 1, "approved"),   # 29 days: inside
        (RETURN_WINDOW_DAYS, "approved"),       # 30 days: policy says "up to 30" -> inclusive
        (RETURN_WINDOW_DAYS + 1, "denied"),     # 31 days: outside
    ],
)
def test_r1_window_boundary_is_inclusive_of_day_30(days_ago, expected):
    customer, order = build(delivered_days_ago=days_ago)
    assert evaluate(customer, order).outcome == expected


def test_r1_delivered_today_is_approved():
    customer, order = build(delivered_days_ago=0)
    assert evaluate(customer, order).outcome == "approved"


# ── R3: damage-report boundary ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "days_ago,expected",
    [
        (DAMAGE_REPORT_WINDOW_DAYS, "approved"),      # 7 days: "within 7" -> inclusive
        (DAMAGE_REPORT_WINDOW_DAYS + 1, "denied"),    # 8 days: too late
    ],
)
def test_r3_damage_window_boundary(days_ago, expected):
    customer, order = build(delivered_days_ago=days_ago)
    assert evaluate(customer, order, reason="defective_or_damaged").outcome == expected


def test_r3_includes_shipping_but_change_of_mind_does_not():
    customer, order = build(delivered_days_ago=2, shipping_fee=7.5)
    damaged = evaluate(customer, order, reason="defective_or_damaged")
    mind = evaluate(customer, order, reason="changed_mind")
    assert damaged.includes_shipping is True
    assert damaged.refund_amount == 57.5
    assert mind.includes_shipping is False
    assert mind.refund_amount == 50.0


# ── R8: auto-approval limit boundary ──────────────────────────────────────────

@pytest.mark.parametrize(
    "price,expected",
    [
        (AUTO_APPROVAL_LIMIT - 0.01, "approved"),          # $399.99
        (AUTO_APPROVAL_LIMIT, "approved"),                 # $400.00 -> "up to $400" is auto
        (AUTO_APPROVAL_LIMIT + 0.01, "needs_human_review"),  # $400.01 -> above the cap
    ],
)
def test_r8_auto_approval_limit_boundary(price, expected):
    items = [
        OrderItem(sku="SKU-1", name="Pricey", category="home_goods", quantity=1, unit_price=price)
    ]
    customer, order = build(items=items, shipping_fee=0.0)
    assert evaluate(customer, order).outcome == expected


def test_r8_triggered_by_total_not_unit_price():
    """Three $150 items exceed the cap together even though each is under it."""
    items = [
        OrderItem(sku=f"SKU-{i}", name="Item", category="home_goods", quantity=1, unit_price=150.0)
        for i in range(3)
    ]
    customer, order = build(items=items, shipping_fee=0.0)
    decision = evaluate(customer, order)
    assert decision.refund_amount == 450.0
    assert decision.outcome == "needs_human_review"


def test_r9_flag_beats_amount_and_is_never_auto_approved():
    """A flagged account needs review even for a trivial in-window refund."""
    customer, order = build(flags=["refund_abuse_watch"])
    decision = evaluate(customer, order)
    assert decision.outcome == "needs_human_review"
    assert decision.requires_customer_confirmation is False


# ── Mixed / partial baskets ───────────────────────────────────────────────────

def test_mixed_basket_refunds_only_eligible_lines():
    items = [
        OrderItem(sku="OK-1", name="Shirt", category="apparel", quantity=1, unit_price=40.0),
        OrderItem(sku="FINAL-1", name="Clearance", category="apparel", quantity=1,
                  unit_price=25.0, final_sale=True),
        OrderItem(sku="GC-1", name="Gift Card", category="gift_card", quantity=1, unit_price=100.0),
    ]
    customer, order = build(items=items)
    decision = evaluate(customer, order)
    assert decision.outcome == "approved"
    assert decision.refund_amount == 40.0  # only the shirt
    assert {a.sku for a in decision.item_assessments if not a.eligible} == {"FINAL-1", "GC-1"}
    assert {c.rule_id for c in decision.rule_citations} >= {"R1", "R4", "R5"}


def test_quantity_is_multiplied_into_line_total():
    items = [
        OrderItem(sku="SKU-1", name="Socks", category="apparel", quantity=3, unit_price=12.0)
    ]
    customer, order = build(items=items)
    assert evaluate(customer, order).refund_amount == 36.0


def test_empty_item_ids_means_whole_order():
    customer, order = build()
    assert evaluate(customer, order, item_ids=[]).refund_amount == 50.0


def test_unknown_sku_is_rejected_not_silently_ignored():
    customer, order = build()
    with pytest.raises(UnknownItemError):
        evaluate(customer, order, item_ids=["NOT-A-SKU"])


def test_sku_matching_is_case_insensitive():
    customer, order = build()
    assert evaluate(customer, order, item_ids=["sku-1"]).refund_amount == 50.0


def test_duplicate_sku_request_does_not_double_count():
    """Asking for the same line twice must not pay it out twice."""
    customer, order = build()
    decision = evaluate(customer, order, item_ids=["SKU-1", "SKU-1"])
    assert decision.refund_amount == 50.0


# ── R7 / already-refunded interactions ────────────────────────────────────────

def test_partially_refunded_order_only_refunds_remaining_line():
    items = [
        OrderItem(sku="A", name="A", category="apparel", quantity=1, unit_price=30.0),
        OrderItem(sku="B", name="B", category="apparel", quantity=1, unit_price=20.0),
    ]
    prior = RefundRecord(
        id="RF-0", order_id="ORD-1", customer_id="CUST-1", item_skus=["A"],
        amount=30.0, reason="changed_mind", processed_at=TODAY.isoformat(),
    )
    customer, order = build(items=items, refunds=[prior])
    decision = evaluate(customer, order)
    assert decision.outcome == "approved"
    assert decision.refund_amount == 20.0  # only B remains


def test_r7_blocks_even_a_damage_claim_on_a_refunded_line():
    prior = RefundRecord(
        id="RF-0", order_id="ORD-1", customer_id="CUST-1", item_skus=["SKU-1"],
        amount=50.0, reason="changed_mind", processed_at=TODAY.isoformat(),
    )
    customer, order = build(delivered_days_ago=2, refunds=[prior])
    decision = evaluate(customer, order, reason="defective_or_damaged")
    assert decision.outcome == "denied"
    assert any(c.rule_id == "R7" for c in decision.rule_citations)


# ── Order status interactions ─────────────────────────────────────────────────

def test_cancellation_of_delivered_order_is_denied():
    customer, order = build(status="delivered")
    assert evaluate(customer, order, reason="order_cancellation").outcome == "denied"


def test_cancellation_of_in_transit_order_is_denied():
    customer, order = build(status="in_transit", delivered_days_ago=None)
    decision = evaluate(customer, order, reason="order_cancellation")
    assert decision.outcome == "denied"
    assert decision.rule_citations[0].rule_id == "R2"


def test_processing_order_change_of_mind_points_at_cancellation():
    customer, order = build(status="processing", delivered_days_ago=None)
    decision = evaluate(customer, order)
    assert decision.outcome == "denied"
    assert "R10" in decision.summary  # nudges toward the cancellation path


def test_high_value_cancellation_still_needs_review():
    items = [
        OrderItem(sku="SKU-1", name="Sofa", category="home_goods", quantity=1, unit_price=900.0)
    ]
    customer, order = build(status="processing", delivered_days_ago=None, items=items)
    decision = evaluate(customer, order, reason="order_cancellation")
    assert decision.outcome == "needs_human_review"


# ── Electronics condition matrix (R6) ─────────────────────────────────────────

@pytest.mark.parametrize(
    "condition,expected_amount",
    [("unopened", 200.0), ("opened", 170.0), ("used", 0.0)],
)
def test_r6_condition_matrix(condition, expected_amount):
    items = [
        OrderItem(sku="E-1", name="Speaker", category="electronics", quantity=1, unit_price=200.0)
    ]
    customer, order = build(items=items)
    decision = evaluate(customer, order, item_condition=condition)
    assert decision.refund_amount == expected_amount


def test_r6_restocking_fee_does_not_apply_to_damage_claims():
    """A damaged speaker is refunded in full regardless of being opened."""
    items = [
        OrderItem(sku="E-1", name="Speaker", category="electronics", quantity=1, unit_price=200.0)
    ]
    customer, order = build(items=items, delivered_days_ago=2, shipping_fee=0.0)
    decision = evaluate(customer, order, reason="defective_or_damaged", item_condition="opened")
    assert decision.refund_amount == 200.0


def test_non_electronics_ignores_condition():
    customer, order = build()
    assert evaluate(customer, order, item_condition="used").refund_amount == 50.0


# ── Invariants that hold across every path ────────────────────────────────────

@pytest.mark.parametrize(
    "reason",
    ["changed_mind", "defective_or_damaged", "wrong_item_received",
     "item_not_received", "order_cancellation"],
)
def test_denied_decisions_never_carry_money_or_confirmation(reason):
    customer, order = build(delivered_days_ago=400, status="delivered")
    decision = evaluate(customer, order, reason=reason)
    if decision.outcome == "denied":
        assert decision.refund_amount == 0.0
        assert decision.requires_customer_confirmation is False


@pytest.mark.parametrize(
    "reason",
    ["changed_mind", "defective_or_damaged", "wrong_item_received",
     "item_not_received", "order_cancellation"],
)
def test_every_decision_cites_at_least_one_rule(reason):
    customer, order = build()
    assert evaluate(customer, order, reason=reason).rule_citations


def test_only_approved_decisions_request_confirmation():
    customer, order = build(flags=["refund_abuse_watch"])
    review = evaluate(customer, order)
    approved = evaluate(*build())
    assert review.requires_customer_confirmation is False
    assert approved.requires_customer_confirmation is True
