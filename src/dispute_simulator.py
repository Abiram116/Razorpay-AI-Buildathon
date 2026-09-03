"""Locally-generated dispute cases for demo and evaluation.

Exists ONLY because Razorpay has no merchant-facing "create dispute" API in
Test Mode or Live Mode (verified in Phase 1 - NOTES.md N-003,
BUILD_LOG.md 2026-09-02-01). Every id this module produces is prefixed
`sim_`, which `dispute_schema.SIMULATED_ID_PATTERN` requires and
`REAL_ID_PATTERN` rejects - a simulated case can never collide with, or be
mistaken for, a genuine Razorpay id anywhere downstream (DB, logs, UI).

This module talks to the SAME `ingest_dispute()` entrypoint a verified
webhook uses (src/ingestion.py), with `source="simulated"` explicit at the
call site - never disguised as `"razorpay_webhook"`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .dispute_schema import (
    DISPUTE_PHASES,
    DISPUTE_STATUSES,
    DisputeEntity,
    PaymentSummary,
    generate_simulated_id,
)


@dataclass(frozen=True)
class SimulatedDisputeSpec:
    """Inputs a caller controls; everything else is filled in with valid,
    clearly-synthetic defaults so a spec only needs to state what matters
    for the scenario being demonstrated."""

    amount: int
    currency: str = "INR"
    reason_code: str = "goods_services_not_provided"
    reason_description: str | None = "Product not received"
    status: str = "open"
    phase: str = "chargeback"
    respond_by_hours_from_now: int = 5 * 24
    payment_method: str = "card"
    linked_real_payment_id: str | None = None
    linked_real_order_id: str | None = None


def build_simulated_case(spec: SimulatedDisputeSpec) -> tuple[DisputeEntity, PaymentSummary]:
    """Construct a schema-valid simulated dispute + its payment.

    If `linked_real_payment_id` is supplied (a genuine pay_... id from a real
    Test Mode payment created in Phase 1), the simulated dispute references
    it - letting a demo show a real Razorpay payment alongside a simulated
    dispute event, without ever inventing a fake pay_/order_ id. Absent that,
    a sim_pay_/sim_order_ id is used, which is unambiguous in logs and the UI.
    """
    if spec.status not in DISPUTE_STATUSES:
        raise ValueError(f"status must be one of {sorted(DISPUTE_STATUSES)}")
    if spec.phase not in DISPUTE_PHASES:
        raise ValueError(f"phase must be one of {sorted(DISPUTE_PHASES)}")
    if spec.amount <= 0:
        raise ValueError("amount must be positive")

    now = int(time.time())
    payment_id = spec.linked_real_payment_id or generate_simulated_id("pay")
    order_id = spec.linked_real_order_id or generate_simulated_id("order")

    payment = PaymentSummary(
        id=payment_id,
        order_id=order_id,
        amount=spec.amount,
        currency=spec.currency,
        status="captured",
        method=spec.payment_method,
        captured=True,
        amount_refunded=0,
        refund_status=None,
        created_at=now - 7 * 24 * 3600,
    )
    dispute = DisputeEntity(
        id=generate_simulated_id("disp"),
        payment_id=payment_id,
        amount=spec.amount,
        currency=spec.currency,
        amount_deducted=0,
        reason_code=spec.reason_code,
        reason_description=spec.reason_description,
        respond_by=now + spec.respond_by_hours_from_now * 3600,
        status=spec.status,
        phase=spec.phase,
        created_at=now,
        submitted_at=None,
    )
    return dispute, payment
