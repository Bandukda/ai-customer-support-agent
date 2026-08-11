"""Deterministic refund policy engine.

Implements rules R1–R11 from ``data/refund_policy.md`` as pure functions.
The LLM never computes eligibility or amounts: it calls tools, and those tools
call this engine. Every decision carries the rule citations that produced it,
so the agent can explain outcomes and the admin dashboard can audit them.
"""

from __future__ import annotations

from datetime import date

from ..models import (
    Customer,
    ItemAssessment,
    ItemCondition,
    Order,
    OrderItem,
    PolicyDecision,
    RefundReason,
    RuleCitation,
)

RETURN_WINDOW_DAYS = 30          # R1
DAMAGE_REPORT_WINDOW_DAYS = 7    # R3
RESTOCKING_FEE_RATE = 0.15       # R6
AUTO_APPROVAL_LIMIT = 400.00     # R8

RULES = {
    "R1": "Change-of-mind returns accepted up to 30 days after delivery.",
    "R2": "Shipped-but-undelivered orders cannot be refunded; wait for delivery.",
    "R3": "Damaged/defective/incorrect items: report within 7 days for a full refund incl. shipping.",
    "R4": "Final-sale and perishable items are not returnable.",
    "R5": "Gift cards are non-refundable.",
    "R6": "Opened electronics incur a 15% restocking fee; used electronics are not returnable.",
    "R7": "An order line that was already refunded cannot be refunded again.",
    "R8": "Refunds above $400.00 require human review.",
    "R9": "Accounts flagged for refund abuse require human review for any refund.",
    "R10": "Orders not yet shipped may be cancelled for a full refund incl. shipping.",
    "R11": "Delivered-but-not-received claims require a carrier investigation (human review).",
}


class UnknownItemError(ValueError):
    """Raised when a requested item SKU does not exist on the order."""


def _money(value: float) -> float:
    return round(value + 1e-9, 2)


def _cite(rule_id: str) -> RuleCitation:
    return RuleCitation(rule_id=rule_id, description=RULES[rule_id])


def evaluate_refund_eligibility(
    customer: Customer,
    order: Order,
    reason: RefundReason,
    item_ids: list[str] | None = None,
    item_condition: ItemCondition = "unopened",
    today: date | None = None,
) -> PolicyDecision:
    # Naive local date on purpose - see CRMService.__init__ for the reasoning.
    today = today or date.today()  # noqa: DTZ011
    items = _select_items(order, item_ids)

    if reason == "order_cancellation":
        return _evaluate_cancellation(customer, order)

    if order.status != "delivered":
        note = (
            " The order has not shipped yet, so a pre-shipment cancellation (R10) is available instead."
            if order.status == "processing"
            else ""
        )
        return PolicyDecision(
            outcome="denied",
            rule_citations=[_cite("R2")],
            summary=f"Order {order.id} is '{order.status}' and cannot be refunded before delivery.{note}",
        )

    if reason == "item_not_received":
        amount = _money(sum(i.line_total for i in items) + order.shipping_fee)
        return PolicyDecision(
            outcome="needs_human_review",
            refund_amount=amount,
            includes_shipping=True,
            rule_citations=[_cite("R11")],
            item_assessments=[
                _assessment(i, eligible=True, amount=i.line_total, note="pending carrier investigation")
                for i in items
            ],
            summary=(
                f"Tracking shows {order.id} as delivered; a carrier investigation is required "
                f"before any refund (potential amount ${amount:.2f})."
            ),
        )

    if reason in ("defective_or_damaged", "wrong_item_received"):
        return _evaluate_seller_fault(customer, order, items, today)

    return _evaluate_change_of_mind(customer, order, items, item_condition, today)


def _select_items(order: Order, item_ids: list[str] | None) -> list[OrderItem]:
    if not item_ids:
        return list(order.items)
    by_sku = {item.sku.upper(): item for item in order.items}
    selected: list[OrderItem] = []
    seen: set[str] = set()
    for sku in item_ids:
        key = sku.strip().upper()
        item = by_sku.get(key)
        if item is None:
            valid = ", ".join(sorted(by_sku))
            raise UnknownItemError(f"SKU '{sku}' is not on order {order.id}. Valid SKUs: {valid}.")
        # Deduplicate: a repeated SKU must not be paid out twice, whether the
        # repetition came from the model or a malformed client request.
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
    return selected


def _assessment(
    item: OrderItem,
    eligible: bool,
    amount: float = 0.0,
    rule_id: str | None = None,
    note: str = "",
) -> ItemAssessment:
    return ItemAssessment(
        sku=item.sku,
        name=item.name,
        quantity=item.quantity,
        eligible=eligible,
        refund_amount=_money(amount),
        rule_id=rule_id,
        note=note,
    )


def _evaluate_cancellation(customer: Customer, order: Order) -> PolicyDecision:
    if order.status == "processing":
        amount = _money(order.items_total + order.shipping_fee)
        decision = PolicyDecision(
            outcome="approved",
            refund_amount=amount,
            refund_method="original_payment",
            includes_shipping=True,
            rule_citations=[_cite("R10")],
            item_assessments=[
                _assessment(i, eligible=True, amount=i.line_total, rule_id="R10") for i in order.items
            ],
            summary=(
                f"Order {order.id} has not shipped; cancellation approved for a full refund "
                f"of ${amount:.2f} including shipping."
            ),
            requires_customer_confirmation=True,
        )
        return _apply_review_gates(decision, customer)
    if order.status == "in_transit":
        return PolicyDecision(
            outcome="denied",
            rule_citations=[_cite("R2")],
            summary=(
                f"Order {order.id} has already shipped and can no longer be cancelled; "
                "it must be received (or refused) first."
            ),
        )
    return PolicyDecision(
        outcome="denied",
        rule_citations=[_cite("R10")],
        summary=f"Order {order.id} was already delivered, so cancellation does not apply; use the return flow instead.",
    )


def _evaluate_seller_fault(
    customer: Customer, order: Order, items: list[OrderItem], today: date
) -> PolicyDecision:
    days = order.days_since_delivery(today)
    if days is not None and days > DAMAGE_REPORT_WINDOW_DAYS:
        return PolicyDecision(
            outcome="denied",
            rule_citations=[_cite("R3")],
            summary=(
                f"The issue was reported {days} days after delivery; R3 claims must be made "
                f"within {DAMAGE_REPORT_WINDOW_DAYS} days."
            ),
        )

    assessments, citations = [], [_cite("R3")]
    refunded = order.refunded_skus()
    for item in items:
        if item.sku in refunded:
            assessments.append(_assessment(item, eligible=False, rule_id="R7", note="already refunded"))
            citations.append(_cite("R7"))
        else:
            assessments.append(_assessment(item, eligible=True, amount=item.line_total, rule_id="R3"))

    eligible_total = sum(a.refund_amount for a in assessments if a.eligible)
    if eligible_total == 0:
        return PolicyDecision(
            outcome="denied",
            rule_citations=_dedupe(citations),
            item_assessments=assessments,
            summary=f"All requested items on {order.id} were already refunded (R7).",
        )

    amount = _money(eligible_total + order.shipping_fee)
    decision = PolicyDecision(
        outcome="approved",
        refund_amount=amount,
        refund_method="original_payment",
        includes_shipping=True,
        rule_citations=_dedupe(citations),
        item_assessments=assessments,
        summary=(
            f"Damaged/incorrect item claim on {order.id} reported within {DAMAGE_REPORT_WINDOW_DAYS} days: "
            f"full refund of ${amount:.2f} including shipping."
        ),
        requires_customer_confirmation=True,
    )
    return _apply_review_gates(decision, customer)


def _evaluate_change_of_mind(
    customer: Customer,
    order: Order,
    items: list[OrderItem],
    item_condition: ItemCondition,
    today: date,
) -> PolicyDecision:
    days = order.days_since_delivery(today)
    if days is not None and days > RETURN_WINDOW_DAYS:
        return PolicyDecision(
            outcome="denied",
            rule_citations=[_cite("R1")],
            summary=(
                f"Order {order.id} was delivered {days} days ago, outside the "
                f"{RETURN_WINDOW_DAYS}-day return window."
            ),
        )

    assessments, citations = [], []
    refunded = order.refunded_skus()
    for item in items:
        if item.sku in refunded:
            assessments.append(_assessment(item, eligible=False, rule_id="R7", note="already refunded"))
            citations.append(_cite("R7"))
        elif item.category == "gift_card":
            assessments.append(_assessment(item, eligible=False, rule_id="R5", note="gift cards are non-refundable"))
            citations.append(_cite("R5"))
        elif item.final_sale or item.category == "perishable":
            note = "final-sale item" if item.final_sale else "perishable item"
            assessments.append(_assessment(item, eligible=False, rule_id="R4", note=note))
            citations.append(_cite("R4"))
        elif item.category == "electronics" and item_condition == "used":
            assessments.append(
                _assessment(item, eligible=False, rule_id="R6", note="used electronics are not returnable")
            )
            citations.append(_cite("R6"))
        elif item.category == "electronics" and item_condition == "opened":
            fee = _money(item.line_total * RESTOCKING_FEE_RATE)
            assessments.append(
                _assessment(
                    item,
                    eligible=True,
                    amount=item.line_total - fee,
                    rule_id="R6",
                    note=f"15% restocking fee (${fee:.2f}) applied for opened electronics",
                )
            )
            citations.append(_cite("R6"))
        else:
            assessments.append(_assessment(item, eligible=True, amount=item.line_total, rule_id="R1"))

    citations.insert(0, _cite("R1"))
    eligible_total = _money(sum(a.refund_amount for a in assessments if a.eligible))
    if eligible_total == 0:
        return PolicyDecision(
            outcome="denied",
            rule_citations=_dedupe(citations),
            item_assessments=assessments,
            summary=f"No items on {order.id} are eligible for a change-of-mind refund.",
        )

    ineligible = [a for a in assessments if not a.eligible]
    partial_note = (
        f" ({len(ineligible)} item(s) excluded — see item breakdown)" if ineligible else ""
    )
    decision = PolicyDecision(
        outcome="approved",
        refund_amount=eligible_total,
        refund_method="original_payment",
        includes_shipping=False,
        rule_citations=_dedupe(citations),
        item_assessments=assessments,
        summary=(
            f"Change-of-mind return on {order.id} within the {RETURN_WINDOW_DAYS}-day window: "
            f"refund of ${eligible_total:.2f} (shipping fees not included){partial_note}."
        ),
        requires_customer_confirmation=True,
    )
    return _apply_review_gates(decision, customer)


def _apply_review_gates(decision: PolicyDecision, customer: Customer) -> PolicyDecision:
    if decision.outcome != "approved":
        return decision
    if "refund_abuse_watch" in customer.flags:
        decision.outcome = "needs_human_review"
        decision.rule_citations.append(_cite("R9"))
        decision.summary += " Account is flagged for refund abuse; human review required (R9)."
        decision.requires_customer_confirmation = False
    elif decision.refund_amount > AUTO_APPROVAL_LIMIT:
        decision.outcome = "needs_human_review"
        decision.rule_citations.append(_cite("R8"))
        decision.summary += (
            f" Amount exceeds the ${AUTO_APPROVAL_LIMIT:.2f} auto-approval limit; human review required (R8)."
        )
        decision.requires_customer_confirmation = False
    return decision


def _dedupe(citations: list[RuleCitation]) -> list[RuleCitation]:
    seen: set[str] = set()
    result = []
    for citation in citations:
        if citation.rule_id not in seen:
            seen.add(citation.rule_id)
            result.append(citation)
    return result
