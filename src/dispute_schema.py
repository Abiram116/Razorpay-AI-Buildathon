"""The dispute entity schema, and the boundary between real and simulated data.

Every field here is taken from Razorpay's documented Dispute entity and the
verified `payment.dispute.created` webhook payload (see NOTES.md N-006 and
BUILD_LOG.md 2026-09-02-01). Nothing here is guessed.

CRITICAL INVARIANT: a real Razorpay id is a prefix plus exactly 14
alphanumeric characters (order_TX6aeI9VxWxNPK, disp_AHfqOvkldwsbqt - both
observed against the live API in Phase 1). Simulated data must NEVER use
those prefixes, so that a simulated case can never be mistaken for a real
Razorpay entity anywhere in the system - logs, database, UI. Simulated ids
use a `sim_` prefix in front of the normal one (sim_disp_..., sim_pay_...).
"""

from __future__ import annotations

import hashlib
import random
import re
import string
from dataclasses import dataclass, field
from typing import Any, Literal

REAL_ID_PATTERN = re.compile(r"^(order|pay|disp|doc)_[A-Za-z0-9]{14}$")
SIMULATED_ID_PATTERN = re.compile(r"^sim_(order|pay|disp)_[A-Za-z0-9_]{6,40}$")


def generate_simulated_id(prefix: str) -> str:
    """Mint a fresh random `sim_` id.

    For ad-hoc simulation (scripts/run_simulator.py) where each run is
    intentionally a NEW dispute. For a fixed dataset that must survive across
    processes and runs, use `derive_simulated_id` instead.
    """
    suffix = "".join(random.choices(string.ascii_letters + string.digits, k=14))
    return f"sim_{prefix}_{suffix}"


def derive_simulated_id(prefix: str, key: str) -> str:
    """A `sim_` id deterministically derived from a stable key.

    Seed/evaluation data must mean the same thing in every process and on
    every run: the script that seeds the database and the script that later
    investigates a case are different processes, so a randomly-minted id
    would simply not match across them. Deriving the id from a stable key
    (e.g. the merchant_order_id) makes "ORD-1001" name exactly one payment
    forever, which is also what Phase 8's evaluation needs to be reproducible.
    """
    digest = hashlib.sha256(f"{prefix}:{key}".encode()).hexdigest()[:14]
    return f"sim_{prefix}_{digest}"

# Documented dispute status values (Disputes Entity reference).
DISPUTE_STATUSES = {"open", "under_review", "won", "lost", "closed"}
# Documented phase values (Disputes Entity reference).
DISPUTE_PHASES = {"fraud", "retrieval", "chargeback", "pre_arbitration", "arbitration"}

# All six documented dispute webhook events (Disputes Webhook Events page).
# created/under_review/won/lost/closed map onto DISPUTE_STATUSES 1:1.
# action_required does not correspond to a documented status value, so it is
# logged as an event but never written into the status field - inventing a
# status value Razorpay doesn't document would violate my own rules.
DISPUTE_EVENTS = {
    "payment.dispute.created": "open",
    "payment.dispute.under_review": "under_review",
    "payment.dispute.won": "won",
    "payment.dispute.lost": "lost",
    "payment.dispute.closed": "closed",
    "payment.dispute.action_required": None,
}

DataSource = Literal["razorpay_webhook", "simulated"]


class DisputeValidationError(ValueError):
    """A dispute/payment payload is missing fields or has out-of-range values.

    Raised for both a malformed real webhook payload and a malformed
    simulator input - the same contract applies to both, which is the point.
    """


@dataclass(frozen=True)
class PaymentSummary:
    id: str
    order_id: str | None
    amount: int
    currency: str
    status: str
    method: str | None
    captured: bool
    amount_refunded: int
    refund_status: str | None
    created_at: int


@dataclass(frozen=True)
class DisputeEntity:
    id: str
    payment_id: str
    amount: int
    currency: str
    amount_deducted: int
    reason_code: str
    respond_by: int
    status: str
    phase: str
    created_at: int
    reason_description: str | None = None
    submitted_at: int | None = None


@dataclass(frozen=True)
class IngestedCase:
    """The record created the moment a dispute enters my system.

    `source` and `is_simulated` travel with the case everywhere downstream -
    the dashboard must always be able to show "Simulated Test Dispute" vs a
    genuine Razorpay dispute, per the project's data-provenance rule.
    """

    dispute: DisputeEntity
    payment: PaymentSummary | None
    source: DataSource
    is_simulated: bool
    raw_event: str | None = None


def _require(payload: dict[str, Any], key: str, label: str) -> Any:
    if key not in payload or payload[key] in (None, ""):
        raise DisputeValidationError(f"{label} is missing required field '{key}'")
    return payload[key]


def _validate_id(value: str, prefix: str, label: str, *, allow_simulated: bool) -> str:
    if REAL_ID_PATTERN.match(value) and value.startswith(f"{prefix}_"):
        return value
    if allow_simulated and SIMULATED_ID_PATTERN.match(value) and value.startswith(f"sim_{prefix}_"):
        return value
    raise DisputeValidationError(
        f"{label} id {value!r} does not match the expected {prefix}_... format"
        + (" (or sim_" + prefix + "_... for simulated data)" if allow_simulated else "")
    )


def parse_payment(payload: dict[str, Any], *, allow_simulated: bool = False) -> PaymentSummary:
    """Parse a Razorpay `payment.entity` object into PaymentSummary.

    Fields and defaults match the verified sample payload exactly - no field
    is invented. `notes`, `email`, `contact` etc. are intentionally excluded:
    Phase 2 only needs enough to route and display the case, not PII.
    """
    pid = _validate_id(_require(payload, "id", "payment"), "pay", "payment", allow_simulated=allow_simulated)
    return PaymentSummary(
        id=pid,
        order_id=payload.get("order_id"),
        amount=int(_require(payload, "amount", "payment")),
        currency=str(_require(payload, "currency", "payment")),
        status=str(_require(payload, "status", "payment")),
        method=payload.get("method"),
        captured=bool(payload.get("captured", False)),
        amount_refunded=int(payload.get("amount_refunded", 0)),
        refund_status=payload.get("refund_status"),
        created_at=int(_require(payload, "created_at", "payment")),
    )


def parse_dispute(payload: dict[str, Any], *, allow_simulated: bool = False) -> DisputeEntity:
    """Parse a Razorpay `dispute.entity` object into DisputeEntity."""
    did = _validate_id(_require(payload, "id", "dispute"), "disp", "dispute", allow_simulated=allow_simulated)
    pay_id = _validate_id(
        _require(payload, "payment_id", "dispute"), "pay", "dispute.payment_id",
        allow_simulated=allow_simulated,
    )
    status = str(_require(payload, "status", "dispute"))
    if status not in DISPUTE_STATUSES:
        raise DisputeValidationError(
            f"dispute.status {status!r} is not one of the documented values {sorted(DISPUTE_STATUSES)}"
        )
    phase = str(_require(payload, "phase", "dispute"))
    if phase not in DISPUTE_PHASES:
        raise DisputeValidationError(
            f"dispute.phase {phase!r} is not one of the documented values {sorted(DISPUTE_PHASES)}"
        )
    amount = int(_require(payload, "amount", "dispute"))
    if amount <= 0:
        raise DisputeValidationError(f"dispute.amount must be positive, got {amount}")

    return DisputeEntity(
        id=did,
        payment_id=pay_id,
        amount=amount,
        currency=str(_require(payload, "currency", "dispute")),
        amount_deducted=int(payload.get("amount_deducted", 0)),
        reason_code=str(_require(payload, "reason_code", "dispute")),
        reason_description=payload.get("reason_description"),
        respond_by=int(_require(payload, "respond_by", "dispute")),
        status=status,
        phase=phase,
        created_at=int(_require(payload, "created_at", "dispute")),
        submitted_at=payload.get("submitted_at"),
    )


def parse_webhook_envelope(body: dict[str, Any]) -> tuple[str, DisputeEntity, PaymentSummary | None]:
    """Parse the outer webhook envelope for a payment.dispute.* event.

    Returns (event_name, dispute, payment). `payment` is None when the event
    payload does not carry a payment object (Razorpay's `contains` list on
    later lifecycle events, e.g. dispute.won, may omit it).
    Raises DisputeValidationError on any structural problem - callers must
    treat this as "reject the webhook", never as "guess and continue".
    """
    if body.get("entity") != "event":
        raise DisputeValidationError(f"expected entity='event', got {body.get('entity')!r}")

    event = _require(body, "event", "webhook envelope")
    if event not in DISPUTE_EVENTS:
        raise DisputeValidationError(f"unrecognised event {event!r}")

    payload = _require(body, "payload", "webhook envelope")
    dispute_container = _require(payload, "dispute", "webhook payload")
    dispute_raw = _require(dispute_container, "entity", "webhook payload.dispute")
    dispute = parse_dispute(dispute_raw)

    payment = None
    payment_container = payload.get("payment")
    if payment_container and payment_container.get("entity"):
        payment = parse_payment(payment_container["entity"])

    return event, dispute, payment
