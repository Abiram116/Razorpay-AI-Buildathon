"""Case state, audit log, and webhook idempotency storage.

This is OUR system's operational database (spec section 14), not the
merchant's business data (that's Phase 3's separate merchant.db). Kept in a
distinct file (`CASE_DB_PATH`, default data/merchant/cases.db) so the two
concerns - "what happened to this dispute in my workflow" vs "what did the
merchant's shop actually do" - never get tangled into one schema.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .dispute_schema import DataSource, IngestedCase

# Spec section 14. Every case starts at INGESTED; only these edges are legal.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "INGESTED": {"ANALYZING"},
    "ANALYZING": {"ANALYSIS_COMPLETE", "PENDING_HUMAN_REVIEW"},  # latter = AI failure fallback
    "ANALYSIS_COMPLETE": {"PENDING_HUMAN_REVIEW"},
    "PENDING_HUMAN_REVIEW": {"APPROVED", "OVERRULED"},
    "APPROVED": {"DRAFTED"},
    "DRAFTED": {"SUBMITTED"},
    "OVERRULED": set(),
    "SUBMITTED": set(),
}


class InvalidStateTransition(RuntimeError):
    """A code path tried to move a case through a transition the spec forbids."""


class DuplicateWebhookEvent(RuntimeError):
    """The exact same webhook delivery (by raw-body hash) was already processed."""


@dataclass(frozen=True)
class CaseRecord:
    dispute_id: str
    payment_id: str
    order_id: str | None
    amount: int
    currency: str
    reason_code: str
    respond_by: int
    dispute_status: str
    phase: str
    case_state: str
    source: DataSource
    is_simulated: bool
    ingested_at: int


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


def init_case_db(db_path: Path) -> None:
    with connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases (
                dispute_id      TEXT PRIMARY KEY,
                payment_id      TEXT NOT NULL,
                order_id        TEXT,
                amount          INTEGER NOT NULL,
                currency        TEXT NOT NULL,
                reason_code     TEXT NOT NULL,
                respond_by      INTEGER NOT NULL,
                dispute_status  TEXT NOT NULL,
                phase           TEXT NOT NULL,
                case_state      TEXT NOT NULL,
                source          TEXT NOT NULL CHECK (source IN ('razorpay_webhook','simulated')),
                is_simulated    INTEGER NOT NULL CHECK (is_simulated IN (0,1)),
                ingested_at     INTEGER NOT NULL,
                raw_dispute_json TEXT NOT NULL,
                raw_payment_json TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                dispute_id      TEXT NOT NULL,
                previous_state  TEXT,
                new_state       TEXT NOT NULL,
                actor           TEXT NOT NULL,
                action          TEXT NOT NULL,
                reason          TEXT,
                timestamp       INTEGER NOT NULL,
                FOREIGN KEY (dispute_id) REFERENCES cases(dispute_id)
            );

            CREATE TABLE IF NOT EXISTS investigations (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                dispute_id        TEXT NOT NULL,
                succeeded         INTEGER NOT NULL CHECK (succeeded IN (0,1)),
                classification    TEXT,
                confidence        REAL,
                recommended_action TEXT,
                failure_reason    TEXT,
                model             TEXT,
                result_json       TEXT NOT NULL,
                created_at        INTEGER NOT NULL,
                FOREIGN KEY (dispute_id) REFERENCES cases(dispute_id)
            );

            CREATE INDEX IF NOT EXISTS idx_investigations_dispute
                ON investigations(dispute_id);

            CREATE TABLE IF NOT EXISTS uploaded_documents (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                dispute_id           TEXT NOT NULL,
                local_path           TEXT NOT NULL,
                document_type        TEXT NOT NULL,
                razorpay_document_id TEXT NOT NULL,
                uploaded_at          INTEGER NOT NULL,
                -- Idempotency: the same local file is never uploaded to
                -- Razorpay twice for the same dispute, so a retry after a
                -- partial failure reuses the document ids already obtained.
                UNIQUE (dispute_id, local_path),
                FOREIGN KEY (dispute_id) REFERENCES cases(dispute_id)
            );

            CREATE TABLE IF NOT EXISTS contest_attempts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                dispute_id    TEXT NOT NULL,
                action        TEXT NOT NULL CHECK (action IN ('local_draft','draft','submit')),
                succeeded     INTEGER NOT NULL CHECK (succeeded IN (0,1)),
                actor         TEXT NOT NULL,
                payload_json  TEXT NOT NULL,
                response_json TEXT,
                error         TEXT,
                created_at    INTEGER NOT NULL,
                FOREIGN KEY (dispute_id) REFERENCES cases(dispute_id)
            );

            CREATE INDEX IF NOT EXISTS idx_contest_attempts_dispute
                ON contest_attempts(dispute_id);

            CREATE TABLE IF NOT EXISTS webhook_events (
                body_hash       TEXT PRIMARY KEY,
                event_type      TEXT NOT NULL,
                dispute_id      TEXT,
                signature_valid INTEGER NOT NULL CHECK (signature_valid IN (0,1)),
                received_at     INTEGER NOT NULL,
                processed       INTEGER NOT NULL DEFAULT 0
            );
            """
        )


def hash_webhook_body(raw_body: bytes) -> str:
    """Idempotency key. Razorpay's envelope carries no delivery/event id
    (confirmed against the documented payload - see NOTES.md), so a retried
    delivery is only distinguishable by its bytes being identical."""
    return hashlib.sha256(raw_body).hexdigest()


def record_webhook_receipt(
    db_path: Path, body_hash: str, event_type: str, dispute_id: str | None, signature_valid: bool
) -> bool:
    """Record a webhook delivery attempt. Returns False if it's a duplicate
    (already recorded), in which case the caller must not reprocess it."""
    with connection(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO webhook_events (body_hash, event_type, dispute_id, "
                "signature_valid, received_at, processed) VALUES (?, ?, ?, ?, ?, 0)",
                (body_hash, event_type, dispute_id, int(signature_valid), int(time.time())),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def mark_webhook_processed(db_path: Path, body_hash: str) -> None:
    with connection(db_path) as conn:
        conn.execute("UPDATE webhook_events SET processed = 1 WHERE body_hash = ?", (body_hash,))


def ingest_case(db_path: Path, case: IngestedCase, actor: str) -> CaseRecord:
    """Create (or, for a Razorpay status-update event, update) a case row.

    A fresh `payment.dispute.created` inserts a new row at state INGESTED.
    A later lifecycle event (won/lost/closed/under_review) for a dispute I
    have not seen `created` for is also accepted and inserted directly - the
    ingest_dispute event may have arrived before I deployed, or the operator
    is replaying historical events; I never want to silently drop a real Razorpay
    dispute.
    """
    now = int(time.time())
    with connection(db_path) as conn:
        existing = conn.execute(
            "SELECT case_state FROM cases WHERE dispute_id = ?", (case.dispute.id,)
        ).fetchone()

        if existing is None:
            conn.execute(
                """INSERT INTO cases
                   (dispute_id, payment_id, order_id, amount, currency, reason_code,
                    respond_by, dispute_status, phase, case_state, source, is_simulated,
                    ingested_at, raw_dispute_json, raw_payment_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'INGESTED', ?, ?, ?, ?, ?)""",
                (
                    case.dispute.id, case.dispute.payment_id,
                    case.payment.order_id if case.payment else None,
                    case.dispute.amount, case.dispute.currency, case.dispute.reason_code,
                    case.dispute.respond_by, case.dispute.status, case.dispute.phase,
                    case.source, int(case.is_simulated), now,
                    json.dumps(case.dispute.__dict__),
                    json.dumps(case.payment.__dict__) if case.payment else None,
                ),
            )
            conn.execute(
                "INSERT INTO audit_log (dispute_id, previous_state, new_state, actor, "
                "action, reason, timestamp) VALUES (?, NULL, 'INGESTED', ?, 'ingest', ?, ?)",
                (case.dispute.id, actor, f"dispute ingested via {case.source}", now),
            )
        else:
            conn.execute(
                "UPDATE cases SET dispute_status = ?, phase = ? WHERE dispute_id = ?",
                (case.dispute.status, case.dispute.phase, case.dispute.id),
            )
            conn.execute(
                "INSERT INTO audit_log (dispute_id, previous_state, new_state, actor, "
                "action, reason, timestamp) SELECT dispute_id, case_state, case_state, ?, "
                "'dispute_status_update', ?, ? FROM cases WHERE dispute_id = ?",
                (actor, f"razorpay dispute_status -> {case.dispute.status}", now, case.dispute.id),
            )

        row = conn.execute("SELECT * FROM cases WHERE dispute_id = ?", (case.dispute.id,)).fetchone()
        return _row_to_record(row)


def transition_case_state(
    db_path: Path, dispute_id: str, new_state: str, actor: str, action: str, reason: str | None = None
) -> CaseRecord:
    with connection(db_path) as conn:
        row = conn.execute("SELECT * FROM cases WHERE dispute_id = ?", (dispute_id,)).fetchone()
        if row is None:
            raise ValueError(f"no case for dispute_id={dispute_id!r}")
        current = row["case_state"]
        if new_state not in ALLOWED_TRANSITIONS.get(current, set()):
            raise InvalidStateTransition(f"{current} -> {new_state} is not an allowed transition")
        conn.execute("UPDATE cases SET case_state = ? WHERE dispute_id = ?", (new_state, dispute_id))
        conn.execute(
            "INSERT INTO audit_log (dispute_id, previous_state, new_state, actor, action, "
            "reason, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dispute_id, current, new_state, actor, action, reason, int(time.time())),
        )
        row = conn.execute("SELECT * FROM cases WHERE dispute_id = ?", (dispute_id,)).fetchone()
        return _row_to_record(row)


def get_case(db_path: Path, dispute_id: str) -> CaseRecord | None:
    with connection(db_path) as conn:
        row = conn.execute("SELECT * FROM cases WHERE dispute_id = ?", (dispute_id,)).fetchone()
        return _row_to_record(row) if row else None


def list_cases(db_path: Path) -> list[CaseRecord]:
    with connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM cases ORDER BY ingested_at DESC").fetchall()
        return [_row_to_record(r) for r in rows]


def get_audit_log(db_path: Path, dispute_id: str) -> list[dict]:
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE dispute_id = ? ORDER BY timestamp ASC", (dispute_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def _row_to_record(row: sqlite3.Row) -> CaseRecord:
    return CaseRecord(
        dispute_id=row["dispute_id"], payment_id=row["payment_id"], order_id=row["order_id"],
        amount=row["amount"], currency=row["currency"], reason_code=row["reason_code"],
        respond_by=row["respond_by"], dispute_status=row["dispute_status"], phase=row["phase"],
        case_state=row["case_state"], source=row["source"], is_simulated=bool(row["is_simulated"]),
        ingested_at=row["ingested_at"],
    )


def save_investigation(db_path: Path, result) -> int:
    """Persist an InvestigationResult or InvestigationFailure.

    Both are stored in the same table with `succeeded` distinguishing them,
    so an audit can always show that a case WAS investigated and failed -
    rather than a failure silently leaving no trace at all.
    """
    payload = result.to_dict()
    succeeded = result.succeeded
    with connection(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO investigations (dispute_id, succeeded, classification,
               confidence, recommended_action, failure_reason, model, result_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.dispute_id,
                int(succeeded),
                payload.get("classification"),
                payload.get("confidence"),
                payload.get("recommended_action"),
                payload.get("failure_reason"),
                payload.get("model"),
                json.dumps(payload),
                result.investigation_timestamp,
            ),
        )
        return int(cursor.lastrowid)


def get_latest_investigation(db_path: Path, dispute_id: str) -> dict | None:
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM investigations WHERE dispute_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (dispute_id,),
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["result"] = json.loads(record.pop("result_json"))
        record["succeeded"] = bool(record["succeeded"])
        return record


def record_uploaded_document(
    db_path: Path, dispute_id: str, local_path: str, document_type: str, razorpay_document_id: str
) -> None:
    with connection(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO uploaded_documents (dispute_id, local_path, "
            "document_type, razorpay_document_id, uploaded_at) VALUES (?, ?, ?, ?, ?)",
            (dispute_id, local_path, document_type, razorpay_document_id, int(time.time())),
        )


def get_uploaded_documents(db_path: Path, dispute_id: str) -> dict[str, dict]:
    """Already-uploaded documents for a dispute, keyed by local path.

    This is what makes an upload retry idempotent: anything already here has
    a Razorpay document id and must not be uploaded again.
    """
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM uploaded_documents WHERE dispute_id = ?", (dispute_id,)
        ).fetchall()
        return {row["local_path"]: dict(row) for row in rows}


def record_contest_attempt(
    db_path: Path,
    dispute_id: str,
    action: str,
    succeeded: bool,
    actor: str,
    payload: dict,
    response: dict | None = None,
    error: str | None = None,
) -> int:
    """Persist every contest attempt - successful or not.

    A failed submission must leave a trace: an auditor needs to see that a
    submission was attempted and why it did not happen.
    """
    with connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO contest_attempts (dispute_id, action, succeeded, actor, "
            "payload_json, response_json, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dispute_id, action, int(succeeded), actor, json.dumps(payload),
                json.dumps(response) if response is not None else None,
                error, int(time.time()),
            ),
        )
        return int(cursor.lastrowid)


def get_contest_attempts(db_path: Path, dispute_id: str) -> list[dict]:
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM contest_attempts WHERE dispute_id = ? ORDER BY created_at ASC, id ASC",
            (dispute_id,),
        ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["succeeded"] = bool(record["succeeded"])
            record["payload"] = json.loads(record.pop("payload_json"))
            raw_response = record.pop("response_json")
            record["response"] = json.loads(raw_response) if raw_response else None
            out.append(record)
        return out


def get_latest_contest_attempt(db_path: Path, dispute_id: str, action: str | None = None) -> dict | None:
    attempts = get_contest_attempts(db_path, dispute_id)
    if action is not None:
        attempts = [a for a in attempts if a["action"] == action]
    return attempts[-1] if attempts else None
