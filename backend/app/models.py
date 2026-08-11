"""Domain models shared across services, tools, and API responses."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

RefundReason = Literal[
    "changed_mind",
    "defective_or_damaged",
    "wrong_item_received",
    "item_not_received",
    "order_cancellation",
]

ItemCondition = Literal["unopened", "opened", "used"]

OrderStatus = Literal["processing", "in_transit", "delivered"]

DecisionOutcome = Literal["approved", "denied", "needs_human_review"]


class OrderItem(BaseModel):
    sku: str
    name: str
    category: str
    quantity: int
    unit_price: float
    final_sale: bool = False

    @property
    def line_total(self) -> float:
        return round(self.unit_price * self.quantity, 2)


class RefundRecord(BaseModel):
    id: str
    order_id: str
    customer_id: str
    item_skus: list[str]
    amount: float
    reason: str
    method: str = "original_payment"
    status: str = "processed"
    processed_at: str


class Order(BaseModel):
    id: str
    customer_id: str
    placed_at: str
    delivered_at: str | None = None
    status: OrderStatus
    payment_method: str
    shipping_fee: float = 0.0
    items: list[OrderItem]
    refunds: list[RefundRecord] = Field(default_factory=list)

    @property
    def items_total(self) -> float:
        return round(sum(item.line_total for item in self.items), 2)

    def days_since_delivery(self, today: date) -> int | None:
        if self.delivered_at is None:
            return None
        return (today - date.fromisoformat(self.delivered_at)).days

    def refunded_skus(self) -> set[str]:
        return {
            sku
            for record in self.refunds
            if record.status == "processed"
            for sku in record.item_skus
        }


class Customer(BaseModel):
    id: str
    name: str
    email: str
    phone: str
    tier: str
    joined_at: str
    flags: list[str] = Field(default_factory=list)
    demo_notes: str = ""
    orders: list[Order]


class RuleCitation(BaseModel):
    rule_id: str
    description: str


class ItemAssessment(BaseModel):
    sku: str
    name: str
    quantity: int
    eligible: bool
    refund_amount: float
    rule_id: str | None = None
    note: str = ""


class PolicyDecision(BaseModel):
    outcome: DecisionOutcome
    refund_amount: float = 0.0
    currency: str = "USD"
    refund_method: str | None = None
    includes_shipping: bool = False
    item_assessments: list[ItemAssessment] = Field(default_factory=list)
    rule_citations: list[RuleCitation] = Field(default_factory=list)
    summary: str
    requires_customer_confirmation: bool = False


class EscalationTicket(BaseModel):
    id: str
    customer_email: str
    order_id: str | None = None
    summary: str
    status: str = "open"
    sla: str = "1 business day"
    created_at: str
