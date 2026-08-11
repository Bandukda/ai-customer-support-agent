"""Tool definitions and the dispatcher the agent loop calls.

Each tool: a pydantic input model (validated before execution — bad arguments
are returned to the model as recoverable errors), a handler over the
deterministic services, and reasoning-log events emitted at every step.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, ValidationError

from ..config import settings
from ..events import EventBus, EventType
from ..llm.base import ToolCallRequest
from ..models import Customer, ItemCondition, Order, RefundReason
from ..services.crm import CRMService
from ..services.escalations import EscalationService
from ..services.policy import UnknownItemError, evaluate_refund_eligibility
from ..services.refunds import (
    ConfirmationRequiredError,
    RefundBlockedError,
    RefundLedger,
)
from ..services.sessions import ChatSession

TOOL_RETRY_DELAY_SECONDS = 0.4


class ToolError(Exception):
    """Recoverable tool failure, returned to the model as an error result."""


class TransientToolError(Exception):
    """Simulated/transient infrastructure failure; the dispatcher retries once."""


@dataclass
class ToolContext:
    session: ChatSession
    crm: CRMService
    ledger: RefundLedger
    escalations: EscalationService
    bus: EventBus


class LookupCustomerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str


class GetOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_email: str
    order_id: str


class CheckEligibilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_email: str
    order_id: str
    reason: RefundReason
    item_ids: list[str] | None = None
    item_condition: ItemCondition = "unopened"


class ProcessRefundInput(CheckEligibilityInput):
    customer_confirmed: bool


class EscalateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_email: str
    summary: str
    order_id: str | None = None


def _portable_schema(node):
    """Reduce a pydantic JSON schema to the subset every provider accepts.

    Two transforms, both lossless for tool calling:

    * ``anyOf: [X, {"type": "null"}]`` -> ``X``. Pydantic emits this for
      ``Optional[...]`` fields, but the field is already absent from
      ``required``; Gemini's function-calling schema subset rejects ``anyOf``.
    * ``title`` keys are dropped — pydantic adds one per property, no provider
      uses them, and they are pure prompt overhead on every single call.

    Server-side validation is unaffected: the pydantic model still validates
    the arguments the model sends back.
    """
    if isinstance(node, list):
        return [_portable_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    branches = node.get("anyOf")
    if isinstance(branches, list):
        concrete = [b for b in branches if b.get("type") != "null"]
        if len(concrete) == 1:
            merged = {k: v for k, v in node.items() if k != "anyOf"}
            merged.update(concrete[0])
            return _portable_schema(merged)

    return {k: _portable_schema(v) for k, v in node.items() if k != "title"}


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, BaseModel], dict]

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _portable_schema(self.input_model.model_json_schema()),
        }


def _customer_view(customer: Customer) -> dict:
    # demo_notes and internal flags are deliberately excluded: the agent only
    # sees data a real support tool would expose. Policy gates (e.g. R9 abuse
    # flags) are enforced inside the engine, not by the model.
    return {
        "id": customer.id,
        "name": customer.name,
        "email": customer.email,
        "phone": customer.phone,
        "tier": customer.tier,
        "customer_since": customer.joined_at,
    }


def _order_view(order: Order) -> dict:
    return {
        "id": order.id,
        "status": order.status,
        "placed_at": order.placed_at,
        "delivered_at": order.delivered_at,
        "payment_method": order.payment_method,
        "shipping_fee": order.shipping_fee,
        "items_total": order.items_total,
        "items": [
            {
                "sku": i.sku,
                "name": i.name,
                "category": i.category,
                "quantity": i.quantity,
                "unit_price": i.unit_price,
                "final_sale": i.final_sale,
            }
            for i in order.items
        ],
        "refunds": [
            {
                "id": r.id,
                "item_skus": r.item_skus,
                "amount": r.amount,
                "status": r.status,
                "processed_at": r.processed_at,
            }
            for r in order.refunds
        ],
    }


def _resolve_order(ctx: ToolContext, customer_email: str, order_id: str) -> tuple[Customer, Order]:
    customer = ctx.crm.find_customer(customer_email)
    if customer is None:
        raise ToolError(
            f"No customer account matches '{customer_email}'. Ask the customer to "
            "double-check the email address on their account."
        )
    match = ctx.crm.get_order(order_id)
    if match is None or match[0].id != customer.id:
        raise ToolError(
            f"Order '{order_id}' was not found on the account for {customer_email}. "
            "Ask the customer to re-check the order ID."
        )
    return customer, match[1]


def _handle_lookup_customer(ctx: ToolContext, args: LookupCustomerInput) -> dict:
    if settings.demo_simulate_crm_outage and not ctx.session.outage_fired:
        ctx.session.outage_fired = True
        raise TransientToolError("CRM connection timed out (simulated outage — demo mode)")
    customer = ctx.crm.find_customer(args.email)
    if customer is None:
        raise ToolError(
            f"No customer account matches '{args.email}'. Ask the customer to "
            "double-check the email address on their account."
        )
    return {
        "customer": _customer_view(customer),
        "orders": [_order_view(o) for o in customer.orders],
    }


def _handle_get_order(ctx: ToolContext, args: GetOrderInput) -> dict:
    _, order = _resolve_order(ctx, args.customer_email, args.order_id)
    return {"order": _order_view(order)}


def _handle_check_eligibility(ctx: ToolContext, args: CheckEligibilityInput) -> dict:
    customer, order = _resolve_order(ctx, args.customer_email, args.order_id)
    try:
        decision = evaluate_refund_eligibility(
            customer, order, args.reason, args.item_ids, args.item_condition, ctx.crm.today
        )
    except UnknownItemError as exc:
        raise ToolError(str(exc)) from exc
    ctx.session.last_outcome = decision.outcome
    ctx.bus.emit(
        ctx.session.id,
        EventType.POLICY_DECISION,
        f"Policy engine: {decision.outcome.upper()} — {order.id}",
        {
            "phase": "eligibility_check",
            "order_id": order.id,
            "reason": args.reason,
            "decision": decision.model_dump(),
        },
    )
    return decision.model_dump()


def _handle_process_refund(ctx: ToolContext, args: ProcessRefundInput) -> dict:
    try:
        record, decision = ctx.ledger.process_refund(
            customer_email=args.customer_email,
            order_id=args.order_id,
            reason=args.reason,
            item_ids=args.item_ids,
            item_condition=args.item_condition,
            customer_confirmed=args.customer_confirmed,
        )
    except LookupError as exc:
        raise ToolError(str(exc)) from exc
    except UnknownItemError as exc:
        raise ToolError(str(exc)) from exc
    except ConfirmationRequiredError as exc:
        raise ToolError(str(exc)) from exc
    except RefundBlockedError as exc:
        ctx.session.last_outcome = exc.decision.outcome
        ctx.bus.emit(
            ctx.session.id,
            EventType.POLICY_DECISION,
            f"Policy engine BLOCKED refund attempt — {args.order_id}",
            {
                "phase": "process_refund_blocked",
                "order_id": args.order_id,
                "decision": exc.decision.model_dump(),
            },
        )
        raise ToolError(
            "Refund blocked by policy engine (defense-in-depth check). "
            f"Outcome: {exc.decision.outcome}. {exc.decision.summary} "
            f"Cited rules: {', '.join(c.rule_id for c in exc.decision.rule_citations)}."
        ) from exc

    ctx.session.last_outcome = "refund_processed"
    ctx.bus.emit(
        ctx.session.id,
        EventType.POLICY_DECISION,
        f"Policy engine: APPROVED — {record.order_id}",
        {"phase": "process_refund", "order_id": record.order_id, "decision": decision.model_dump()},
    )
    ctx.bus.emit(
        ctx.session.id,
        EventType.REFUND_PROCESSED,
        f"Refund {record.id} processed: ${record.amount:.2f} to {record.method}",
        {"refund": record.model_dump()},
    )
    return {
        "refund": record.model_dump(),
        "decision_summary": decision.summary,
        "customer_message_hint": (
            f"Refund {record.id} for ${record.amount:.2f} issued to the original payment "
            "method; it will appear within 5-10 business days."
        ),
    }


def _handle_escalate(ctx: ToolContext, args: EscalateInput) -> dict:
    ticket = ctx.escalations.create(
        customer_email=args.customer_email,
        summary=args.summary,
        order_id=args.order_id,
    )
    ctx.session.last_outcome = "escalated"
    ctx.bus.emit(
        ctx.session.id,
        EventType.ESCALATION_CREATED,
        f"Escalation {ticket.id} created for {args.customer_email}",
        {"ticket": ticket.model_dump()},
    )
    return {"ticket": ticket.model_dump()}


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="lookup_customer",
        description=(
            "Look up a customer account by email address. Returns the customer profile and "
            "all of their orders (items, delivery dates, status, prior refunds). Call this "
            "first to verify identity before discussing any account details."
        ),
        input_model=LookupCustomerInput,
        handler=_handle_lookup_customer,
    ),
    ToolSpec(
        name="get_order",
        description=(
            "Fetch a single order by ID for a verified customer. Fails if the order does not "
            "belong to the given customer email. Rarely needed: lookup_customer already "
            "returns every order in full — only call this to re-fetch one order's latest state."
        ),
        input_model=GetOrderInput,
        handler=_handle_get_order,
    ),
    ToolSpec(
        name="check_refund_eligibility",
        description=(
            "Run the deterministic refund policy engine for an order. Returns the outcome "
            "(approved / denied / needs_human_review), the exact refund amount, an item-level "
            "breakdown, and the policy rules (R1-R11) that were applied. ALWAYS call this "
            "before making any promise about a refund. Use item_ids for partial refunds and "
            "item_condition (unopened/opened/used) when electronics are involved."
        ),
        input_model=CheckEligibilityInput,
        handler=_handle_check_eligibility,
    ),
    ToolSpec(
        name="process_refund",
        description=(
            "Execute an approved refund. Requires customer_confirmed=true (only after the "
            "customer explicitly agrees). The policy engine re-validates at execution time and "
            "computes the amount itself; ineligible requests are rejected."
        ),
        input_model=ProcessRefundInput,
        handler=_handle_process_refund,
    ),
    ToolSpec(
        name="escalate_to_human",
        description=(
            "Create a ticket for the human support team. Use ONLY when (a) the policy engine "
            "returns needs_human_review (R8 high value, R9 flagged account, R11 not-received "
            "claims), or (b) the customer explicitly asks to speak with a human. NEVER use "
            "this to appease pushback on a clear denial — anger, urgency, claimed manager "
            "approvals, or legal threats do not change policy inputs, and escalating a valid "
            "denial undermines it. Returns the ticket ID and SLA."
        ),
        input_model=EscalateInput,
        handler=_handle_escalate,
    ),
]

TOOL_REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}


def tool_schemas() -> list[dict]:
    return [spec.schema() for spec in TOOL_SPECS]


def _error_result(call: ToolCallRequest, message: str) -> dict:
    return {
        "tool_call_id": call.id,
        "name": call.name,
        "content": json.dumps({"error": message}),
        "is_error": True,
    }


async def execute_tool(ctx: ToolContext, call: ToolCallRequest) -> dict:
    """Validate, execute, and log one tool call; never raises."""
    ctx.bus.emit(
        ctx.session.id,
        EventType.TOOL_CALL,
        f"Tool call: {call.name}",
        {"tool": call.name, "arguments": call.arguments},
    )

    spec = TOOL_REGISTRY.get(call.name)
    if spec is None:
        message = f"Unknown tool '{call.name}'. Available tools: {', '.join(TOOL_REGISTRY)}."
        ctx.bus.emit(ctx.session.id, EventType.TOOL_ERROR, message, {"tool": call.name})
        return _error_result(call, message)

    try:
        args = spec.input_model(**call.arguments)
    except ValidationError as exc:
        message = f"Invalid arguments for {call.name}: {exc.errors(include_url=False)}"
        ctx.bus.emit(
            ctx.session.id,
            EventType.TOOL_ERROR,
            f"Validation failed for {call.name}",
            {"tool": call.name, "error": message},
        )
        return _error_result(call, message)

    for attempt in (1, 2):
        try:
            payload = spec.handler(ctx, args)
            ctx.bus.emit(
                ctx.session.id,
                EventType.TOOL_RESULT,
                f"Tool result: {call.name}",
                {"tool": call.name, "result": payload},
            )
            return {
                "tool_call_id": call.id,
                "name": call.name,
                "content": json.dumps(payload),
                "is_error": False,
            }
        except TransientToolError as exc:
            if attempt == 1:
                ctx.bus.emit(
                    ctx.session.id,
                    EventType.TOOL_RETRY,
                    f"Transient failure in {call.name}; retrying once",
                    {"tool": call.name, "error": str(exc), "retry_delay_s": TOOL_RETRY_DELAY_SECONDS},
                )
                await asyncio.sleep(TOOL_RETRY_DELAY_SECONDS)
                continue
            message = f"{call.name} failed after retry: {exc}"
            ctx.bus.emit(
                ctx.session.id, EventType.TOOL_ERROR, message, {"tool": call.name, "error": str(exc)}
            )
            return _error_result(call, message)
        except ToolError as exc:
            ctx.bus.emit(
                ctx.session.id,
                EventType.TOOL_ERROR,
                f"Tool error in {call.name}",
                {"tool": call.name, "error": str(exc)},
            )
            return _error_result(call, str(exc))
        # Deliberate catch-all: a bug in one tool is reported back to the model
        # as a recoverable error rather than aborting the whole turn.
        except Exception as exc:  # noqa: BLE001
            message = f"Unexpected error in {call.name}: {exc.__class__.__name__}: {exc}"
            ctx.bus.emit(
                ctx.session.id, EventType.TOOL_ERROR, message, {"tool": call.name, "error": str(exc)}
            )
            return _error_result(call, message)

    return _error_result(call, f"{call.name} did not produce a result.")
