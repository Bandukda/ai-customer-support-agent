"""Mock CRM backed by a JSON seed file.

Seed dates are stored as ``*_days_ago`` offsets and hydrated to ISO dates at
load time, relative to "today" — this keeps every demo scenario (30-day window,
7-day damage window, etc.) valid no matter when the app is run.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from ..models import Customer, Order, OrderItem, RefundRecord


class CRMService:
    def __init__(self, data_path: Path, today: date | None = None) -> None:
        # A naive local date is deliberate: "30 days after delivery" is a
        # consumer-facing calendar window, and UTC would shift the boundary
        # for customers. Tests always inject `today` explicitly.
        self.today = today or date.today()  # noqa: DTZ011
        self._customers: list[Customer] = []
        self._by_email: dict[str, Customer] = {}
        self._orders: dict[str, tuple[Customer, Order]] = {}
        self._load(data_path)

    def _load(self, data_path: Path) -> None:
        raw = json.loads(data_path.read_text(encoding="utf-8"))
        for entry in raw["customers"]:
            customer = self._hydrate_customer(entry)
            self._customers.append(customer)
            self._by_email[customer.email.lower()] = customer
            for order in customer.orders:
                self._orders[order.id.upper()] = (customer, order)

    def _hydrate_customer(self, entry: dict) -> Customer:
        orders = [self._hydrate_order(o, entry["id"]) for o in entry["orders"]]
        return Customer(
            id=entry["id"],
            name=entry["name"],
            email=entry["email"],
            phone=entry["phone"],
            tier=entry["tier"],
            joined_at=self._offset_to_date(entry["joined_days_ago"]),
            flags=entry.get("flags", []),
            demo_notes=entry.get("demo_notes", ""),
            orders=orders,
        )

    def _hydrate_order(self, entry: dict, customer_id: str) -> Order:
        delivered_days = entry.get("delivered_days_ago")
        refunds = [
            RefundRecord(
                id=r["id"],
                order_id=entry["id"],
                customer_id=customer_id,
                item_skus=r["item_skus"],
                amount=r["amount"],
                reason=r["reason"],
                method=r.get("method", "original_payment"),
                status=r.get("status", "processed"),
                processed_at=self._offset_to_date(r["processed_days_ago"]),
            )
            for r in entry.get("refunds", [])
        ]
        return Order(
            id=entry["id"],
            customer_id=customer_id,
            placed_at=self._offset_to_date(entry["placed_days_ago"]),
            delivered_at=(
                self._offset_to_date(delivered_days) if delivered_days is not None else None
            ),
            status=entry["status"],
            payment_method=entry["payment_method"],
            shipping_fee=entry.get("shipping_fee", 0.0),
            items=[OrderItem(**item) for item in entry["items"]],
            refunds=refunds,
        )

    def _offset_to_date(self, days_ago: int) -> str:
        return (self.today - timedelta(days=days_ago)).isoformat()

    def find_customer(self, email: str) -> Customer | None:
        return self._by_email.get(email.strip().lower())

    def get_order(self, order_id: str) -> tuple[Customer, Order] | None:
        return self._orders.get(order_id.strip().upper())

    def all_customers(self) -> list[Customer]:
        return self._customers

    def add_refund(self, order: Order, record: RefundRecord) -> None:
        order.refunds.append(record)
