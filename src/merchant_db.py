"""The merchant's own operational data - NOT Razorpay data.

This represents what a merchant's internal systems would hold: fulfilment,
courier tracking, support conversations, refunds, store policy, and business
records. Razorpay has none of this (it only knows about the payment), which
is exactly why it exists as a separate store from `src/database.py` (my
workflow/case state) and from anything Razorpay's API returns.

Every id that references a Razorpay-shaped entity (razorpay_order_id,
payment_id) is validated against the SAME real-vs-simulated boundary defined
in dispute_schema.py - a merchant order can point at a genuine Razorpay test
order/payment (REAL_ID_PATTERN) or a labelled simulated one
(SIMULATED_ID_PATTERN), never anything else. `merchant_order_id` itself is
my own internal identifier (ORD-####) and deliberately does NOT look like a
Razorpay id, so the three id families are never visually confusable.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .dispute_schema import REAL_ID_PATTERN, SIMULATED_ID_PATTERN

# Mirrors Razorpay's documented dispute evidence categories exactly (Contest a
# Dispute API / Disputes Entity - see docs fetched in Phase 1), so Phase 5's
# evidence builder can map a merchant document straight onto a Razorpay
# evidence field without a second translation table.
EVIDENCE_DOCUMENT_TYPES = {
    "shipping_proof", "billing_proof", "cancellation_proof",
    "customer_communication", "proof_of_service", "explanation_letter",
    "refund_confirmation", "access_activity_log", "refund_cancellation_policy",
    "term_and_conditions", "others",
}

ORDER_STATUSES = {"confirmed", "fulfilled", "cancelled", "refunded"}
PRODUCT_TYPES = {"physical", "digital", "service"}
DELIVERY_STATUSES = {
    "not_applicable", "never_shipped", "in_transit", "delivered",
    "delivery_failed", "returned_to_sender",
}
REFUND_STATUSES = {"none", "pending", "processed", "rejected"}
COMM_CHANNELS = {"email", "chat", "phone", "sms", "support_ticket"}
COMM_DIRECTIONS = {"inbound", "outbound"}
POLICY_TYPES = {"refund_policy", "cancellation_policy", "terms_and_conditions"}


class MerchantDataError(ValueError):
    """A merchant-side record is malformed or references an invalid id."""


def _validate_razorpay_ref(value: str | None, prefix: str, label: str) -> None:
    """A stored razorpay_order_id/payment_id must be a genuine Razorpay id or
    an explicitly labelled simulated one - never an ad-hoc string that could
    be mistaken for either."""
    if value is None:
        return
    is_real = bool(REAL_ID_PATTERN.match(value)) and value.startswith(f"{prefix}_")
    is_sim = bool(SIMULATED_ID_PATTERN.match(value)) and value.startswith(f"sim_{prefix}_")
    if not (is_real or is_sim):
        raise MerchantDataError(
            f"{label} {value!r} is neither a valid {prefix}_... Razorpay id "
            f"nor a valid sim_{prefix}_... simulated id"
        )


@dataclass(frozen=True)
class Order:
    merchant_order_id: str
    razorpay_order_id: str | None
    payment_id: str | None
    customer_id: str
    product: str
    product_type: str
    amount: int
    currency: str
    order_timestamp: int
    order_status: str
    shipping_address: str | None
    billing_address: str | None
    is_simulated: bool


@dataclass(frozen=True)
class Shipment:
    merchant_order_id: str
    tracking_id: str | None
    courier: str | None
    shipped_at: int | None
    delivered_at: int | None
    delivery_status: str
    delivery_location: str | None
    recipient_confirmation: str | None


@dataclass(frozen=True)
class Communication:
    merchant_order_id: str
    customer_id: str
    timestamp: int
    channel: str
    message: str
    direction: str
    # DB row id, populated on read. Trailing+optional so existing positional
    # construction (seed data, tests) is unaffected. Needed because there can
    # be many communications per order, so Phase 4's evidence citations have
    # to be able to point at ONE specific message, not just "a conversation".
    id: int | None = None


@dataclass(frozen=True)
class Refund:
    merchant_order_id: str
    payment_id: str | None
    refund_requested: bool
    refund_status: str
    refund_amount: int | None
    refund_timestamp: int | None
    reason: str | None


@dataclass(frozen=True)
class Policy:
    policy_type: str
    version: str
    effective_from: int
    content: str


@dataclass(frozen=True)
class EvidenceDocument:
    merchant_order_id: str
    document_type: str
    filename: str
    description: str
    # See Communication.id - same reasoning, documents are also 0..n per order.
    id: int | None = None


@dataclass(frozen=True)
class CaseEvidence:
    """Everything the merchant's systems can offer about one order, bundled
    for Phase 4 (AI investigation) and Phase 5 (evidence builder) to consume
    with a single lookup. Any field can legitimately be absent - e.g. a
    digital order has no shipment - and callers (including the AI prompt
    builder) must treat absence as "no such record", never as "the merchant
    is hiding something". Deciding what that absence MEANS is a Phase 4
    reasoning concern, not something this module resolves for them."""

    order: Order
    shipment: Shipment | None
    communications: list[Communication]
    refund: Refund | None
    documents: list[EvidenceDocument]
    policies: list[Policy]


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_merchant_db(db_path: Path) -> None:
    with connection(db_path) as conn:
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS orders (
                merchant_order_id  TEXT PRIMARY KEY,
                razorpay_order_id  TEXT,
                payment_id         TEXT,
                customer_id        TEXT NOT NULL,
                product            TEXT NOT NULL,
                product_type       TEXT NOT NULL CHECK (product_type IN {tuple(PRODUCT_TYPES)}),
                amount             INTEGER NOT NULL CHECK (amount > 0),
                currency           TEXT NOT NULL,
                order_timestamp    INTEGER NOT NULL,
                order_status       TEXT NOT NULL CHECK (order_status IN {tuple(ORDER_STATUSES)}),
                shipping_address   TEXT,
                billing_address    TEXT,
                is_simulated       INTEGER NOT NULL CHECK (is_simulated IN (0,1))
            );

            CREATE INDEX IF NOT EXISTS idx_orders_payment_id ON orders(payment_id);
            CREATE INDEX IF NOT EXISTS idx_orders_razorpay_order_id ON orders(razorpay_order_id);
            CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id);

            CREATE TABLE IF NOT EXISTS shipments (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant_order_id      TEXT NOT NULL UNIQUE,
                tracking_id            TEXT,
                courier                TEXT,
                shipped_at             INTEGER,
                delivered_at           INTEGER,
                delivery_status        TEXT NOT NULL CHECK (delivery_status IN {tuple(DELIVERY_STATUSES)}),
                delivery_location      TEXT,
                recipient_confirmation TEXT,
                FOREIGN KEY (merchant_order_id) REFERENCES orders(merchant_order_id)
            );

            CREATE TABLE IF NOT EXISTS customer_communications (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant_order_id  TEXT NOT NULL,
                customer_id        TEXT NOT NULL,
                timestamp          INTEGER NOT NULL,
                channel            TEXT NOT NULL CHECK (channel IN {tuple(COMM_CHANNELS)}),
                message            TEXT NOT NULL,
                direction          TEXT NOT NULL CHECK (direction IN {tuple(COMM_DIRECTIONS)}),
                FOREIGN KEY (merchant_order_id) REFERENCES orders(merchant_order_id)
            );

            CREATE INDEX IF NOT EXISTS idx_comms_order ON customer_communications(merchant_order_id);

            CREATE TABLE IF NOT EXISTS refunds (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant_order_id  TEXT NOT NULL UNIQUE,
                payment_id         TEXT,
                refund_requested   INTEGER NOT NULL CHECK (refund_requested IN (0,1)),
                refund_status      TEXT NOT NULL CHECK (refund_status IN {tuple(REFUND_STATUSES)}),
                refund_amount      INTEGER,
                refund_timestamp   INTEGER,
                reason             TEXT,
                FOREIGN KEY (merchant_order_id) REFERENCES orders(merchant_order_id)
            );

            CREATE TABLE IF NOT EXISTS policies (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                policy_type        TEXT NOT NULL CHECK (policy_type IN {tuple(POLICY_TYPES)}),
                version            TEXT NOT NULL,
                effective_from     INTEGER NOT NULL,
                content            TEXT NOT NULL,
                UNIQUE (policy_type, version)
            );

            CREATE TABLE IF NOT EXISTS documents (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant_order_id  TEXT NOT NULL,
                document_type      TEXT NOT NULL CHECK (document_type IN {tuple(EVIDENCE_DOCUMENT_TYPES)}),
                filename           TEXT NOT NULL,
                description        TEXT NOT NULL,
                FOREIGN KEY (merchant_order_id) REFERENCES orders(merchant_order_id)
            );

            CREATE INDEX IF NOT EXISTS idx_documents_order ON documents(merchant_order_id);
            """
        )


# ----------------------------------------------------------------------
# writes
# ----------------------------------------------------------------------

def insert_order(db_path: Path, order: Order) -> None:
    _validate_razorpay_ref(order.razorpay_order_id, "order", "razorpay_order_id")
    _validate_razorpay_ref(order.payment_id, "pay", "payment_id")
    with connection(db_path) as conn:
        conn.execute(
            """INSERT INTO orders (merchant_order_id, razorpay_order_id, payment_id,
               customer_id, product, product_type, amount, currency, order_timestamp,
               order_status, shipping_address, billing_address, is_simulated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order.merchant_order_id, order.razorpay_order_id, order.payment_id,
             order.customer_id, order.product, order.product_type, order.amount,
             order.currency, order.order_timestamp, order.order_status,
             order.shipping_address, order.billing_address, int(order.is_simulated)),
        )


def insert_shipment(db_path: Path, shipment: Shipment) -> None:
    with connection(db_path) as conn:
        conn.execute(
            """INSERT INTO shipments (merchant_order_id, tracking_id, courier, shipped_at,
               delivered_at, delivery_status, delivery_location, recipient_confirmation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (shipment.merchant_order_id, shipment.tracking_id, shipment.courier,
             shipment.shipped_at, shipment.delivered_at, shipment.delivery_status,
             shipment.delivery_location, shipment.recipient_confirmation),
        )


def insert_communication(db_path: Path, comm: Communication) -> None:
    with connection(db_path) as conn:
        conn.execute(
            """INSERT INTO customer_communications
               (merchant_order_id, customer_id, timestamp, channel, message, direction)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (comm.merchant_order_id, comm.customer_id, comm.timestamp,
             comm.channel, comm.message, comm.direction),
        )


def insert_refund(db_path: Path, refund: Refund) -> None:
    _validate_razorpay_ref(refund.payment_id, "pay", "refund.payment_id")
    with connection(db_path) as conn:
        conn.execute(
            """INSERT INTO refunds (merchant_order_id, payment_id, refund_requested,
               refund_status, refund_amount, refund_timestamp, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (refund.merchant_order_id, refund.payment_id, int(refund.refund_requested),
             refund.refund_status, refund.refund_amount, refund.refund_timestamp,
             refund.reason),
        )


def insert_policy(db_path: Path, policy: Policy) -> None:
    with connection(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO policies (policy_type, version, effective_from, content)
               VALUES (?, ?, ?, ?)""",
            (policy.policy_type, policy.version, policy.effective_from, policy.content),
        )


def insert_document(db_path: Path, document: EvidenceDocument) -> None:
    with connection(db_path) as conn:
        conn.execute(
            """INSERT INTO documents (merchant_order_id, document_type, filename, description)
               VALUES (?, ?, ?, ?)""",
            (document.merchant_order_id, document.document_type,
             document.filename, document.description),
        )


# ----------------------------------------------------------------------
# reads
# ----------------------------------------------------------------------

def get_order(db_path: Path, merchant_order_id: str) -> Order | None:
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE merchant_order_id = ?", (merchant_order_id,)
        ).fetchone()
        return _row_to_order(row) if row else None


def get_order_by_payment_id(db_path: Path, payment_id: str) -> Order | None:
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE payment_id = ?", (payment_id,)
        ).fetchone()
        return _row_to_order(row) if row else None


def get_order_by_razorpay_order_id(db_path: Path, razorpay_order_id: str) -> Order | None:
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE razorpay_order_id = ?", (razorpay_order_id,)
        ).fetchone()
        return _row_to_order(row) if row else None


def get_shipment(db_path: Path, merchant_order_id: str) -> Shipment | None:
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM shipments WHERE merchant_order_id = ?", (merchant_order_id,)
        ).fetchone()
        return _row_to_shipment(row) if row else None


def get_communications(db_path: Path, merchant_order_id: str) -> list[Communication]:
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM customer_communications WHERE merchant_order_id = ? "
            "ORDER BY timestamp ASC", (merchant_order_id,),
        ).fetchall()
        return [_row_to_communication(r) for r in rows]


def get_refund(db_path: Path, merchant_order_id: str) -> Refund | None:
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM refunds WHERE merchant_order_id = ?", (merchant_order_id,)
        ).fetchone()
        return _row_to_refund(row) if row else None


def get_documents(db_path: Path, merchant_order_id: str) -> list[EvidenceDocument]:
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE merchant_order_id = ?", (merchant_order_id,)
        ).fetchall()
        return [_row_to_document(r) for r in rows]


def get_active_policies(db_path: Path) -> list[Policy]:
    """The current (most recent effective_from) version of each policy type."""
    with connection(db_path) as conn:
        rows = conn.execute(
            """SELECT p.* FROM policies p
               INNER JOIN (
                   SELECT policy_type, MAX(effective_from) AS latest
                   FROM policies GROUP BY policy_type
               ) latest_p ON p.policy_type = latest_p.policy_type
                          AND p.effective_from = latest_p.latest"""
        ).fetchall()
        return [_row_to_policy(r) for r in rows]


def get_case_evidence(db_path: Path, *, payment_id: str) -> CaseEvidence | None:
    """The single lookup Phase 4/5 need: everything the merchant's systems
    can offer about the order behind a given (real or simulated) payment_id.
    Returns None if I have no merchant-side record of this payment at all -
    the caller (AI investigation) must treat that as "insufficient evidence",
    never guess at what the order might have contained."""
    order = get_order_by_payment_id(db_path, payment_id)
    if order is None:
        return None
    return CaseEvidence(
        order=order,
        shipment=get_shipment(db_path, order.merchant_order_id),
        communications=get_communications(db_path, order.merchant_order_id),
        refund=get_refund(db_path, order.merchant_order_id),
        documents=get_documents(db_path, order.merchant_order_id),
        policies=get_active_policies(db_path),
    )


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        merchant_order_id=row["merchant_order_id"], razorpay_order_id=row["razorpay_order_id"],
        payment_id=row["payment_id"], customer_id=row["customer_id"], product=row["product"],
        product_type=row["product_type"], amount=row["amount"], currency=row["currency"],
        order_timestamp=row["order_timestamp"], order_status=row["order_status"],
        shipping_address=row["shipping_address"], billing_address=row["billing_address"],
        is_simulated=bool(row["is_simulated"]),
    )


def _row_to_shipment(row: sqlite3.Row) -> Shipment:
    return Shipment(
        merchant_order_id=row["merchant_order_id"], tracking_id=row["tracking_id"],
        courier=row["courier"], shipped_at=row["shipped_at"], delivered_at=row["delivered_at"],
        delivery_status=row["delivery_status"], delivery_location=row["delivery_location"],
        recipient_confirmation=row["recipient_confirmation"],
    )


def _row_to_communication(row: sqlite3.Row) -> Communication:
    return Communication(
        merchant_order_id=row["merchant_order_id"], customer_id=row["customer_id"],
        timestamp=row["timestamp"], channel=row["channel"], message=row["message"],
        direction=row["direction"], id=row["id"],
    )


def _row_to_refund(row: sqlite3.Row) -> Refund:
    return Refund(
        merchant_order_id=row["merchant_order_id"], payment_id=row["payment_id"],
        refund_requested=bool(row["refund_requested"]), refund_status=row["refund_status"],
        refund_amount=row["refund_amount"], refund_timestamp=row["refund_timestamp"],
        reason=row["reason"],
    )


def _row_to_policy(row: sqlite3.Row) -> Policy:
    return Policy(
        policy_type=row["policy_type"], version=row["version"],
        effective_from=row["effective_from"], content=row["content"],
    )


def _row_to_document(row: sqlite3.Row) -> EvidenceDocument:
    return EvidenceDocument(
        merchant_order_id=row["merchant_order_id"], document_type=row["document_type"],
        filename=row["filename"], description=row["description"], id=row["id"],
    )
