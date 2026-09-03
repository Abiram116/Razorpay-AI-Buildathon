"""Phase 2: HMAC verification, raw-body handling, idempotency, safe failure.

Uses a locally-computed HMAC-SHA256 signature - the exact algorithm Razorpay
documents (secret as HMAC key, raw body as message, hex digest) - so these
tests exercise the real verification path without needing network access or
a live Razorpay webhook delivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import src.webhook_handler as webhook_handler
from src import database
from src.config import Settings

WEBHOOK_SECRET = "test_webhook_secret_value"

SAMPLE_ENVELOPE = {
    "entity": "event",
    "account_id": "acc_CFvOKjkTwf3GQy",
    "event": "payment.dispute.created",
    "contains": ["payment", "dispute"],
    "payload": {
        "payment": {"entity": {
            "id": "pay_EFtmUsbwpXwBHI", "entity": "payment", "amount": 5297600,
            "currency": "INR", "status": "captured", "order_id": "order_EFtkA6f5jdkfud",
            "international": False, "method": "card", "amount_refunded": 700000,
            "refund_status": "partial", "captured": True, "created_at": 1581525157,
        }},
        "dispute": {"entity": {
            "id": "disp_EsIAlDcoUr8CaQ", "entity": "dispute", "payment_id": "pay_EFtmUsbwpXwBHI",
            "amount": 39000, "currency": "INR", "amount_deducted": 0,
            "reason_code": "processed_invalid_expired_card", "respond_by": 1590431400,
            "status": "open", "phase": "chargeback", "created_at": 1589907957,
        }},
    },
    "created_at": 1589907977,
}


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Point the app at a throwaway DB and a known webhook secret."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abcdefghijklmn")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "testsecret")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)

    db_path = tmp_path / "cases.db"
    settings = webhook_handler.load_settings(require_razorpay=True)
    settings = Settings(
        razorpay=settings.razorpay, ai=settings.ai, deadlines=settings.deadlines,
        costs=settings.costs,
        paths=type(settings.paths)(
            merchant_db=tmp_path / "merchant.db", case_db=db_path,
            generated_docs=tmp_path / "docs",
        ),
    )
    webhook_handler._settings = settings
    webhook_handler._client = None
    database.init_case_db(db_path)

    with TestClient(webhook_handler.app) as c:
        yield c, db_path

    webhook_handler._settings = None
    webhook_handler._client = None


def test_valid_signature_processes_and_creates_case(client):
    c, db_path = client
    body = json.dumps(SAMPLE_ENVELOPE).encode()
    resp = c.post(
        "/webhooks/razorpay/disputes", content=body,
        headers={"X-Razorpay-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processed"
    case = database.get_case(db_path, "disp_EsIAlDcoUr8CaQ")
    assert case is not None
    assert case.case_state == "INGESTED"
    assert case.source == "razorpay_webhook"
    assert case.is_simulated is False


def test_invalid_signature_is_rejected_and_not_processed(client):
    c, db_path = client
    body = json.dumps(SAMPLE_ENVELOPE).encode()
    resp = c.post(
        "/webhooks/razorpay/disputes", content=body,
        headers={"X-Razorpay-Signature": "deadbeef" * 8, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert database.get_case(db_path, "disp_EsIAlDcoUr8CaQ") is None


def test_tampered_body_is_rejected_even_with_a_valid_looking_signature(client):
    c, db_path = client
    body = json.dumps(SAMPLE_ENVELOPE).encode()
    signature = _sign(body)  # signed over the ORIGINAL body
    tampered = json.dumps({**SAMPLE_ENVELOPE, "created_at": 999}).encode()
    resp = c.post(
        "/webhooks/razorpay/disputes", content=tampered,
        headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert database.get_case(db_path, "disp_EsIAlDcoUr8CaQ") is None


def test_missing_signature_header_is_rejected(client):
    c, _ = client
    body = json.dumps(SAMPLE_ENVELOPE).encode()
    resp = c.post("/webhooks/razorpay/disputes", content=body)
    assert resp.status_code == 400


def test_duplicate_delivery_is_processed_once(client):
    c, db_path = client
    body = json.dumps(SAMPLE_ENVELOPE).encode()
    headers = {"X-Razorpay-Signature": _sign(body), "Content-Type": "application/json"}

    first = c.post("/webhooks/razorpay/disputes", content=body, headers=headers)
    second = c.post("/webhooks/razorpay/disputes", content=body, headers=headers)

    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "duplicate_ignored"
    # exactly one audit_log 'ingest' entry despite two deliveries
    log = database.get_audit_log(db_path, "disp_EsIAlDcoUr8CaQ")
    assert sum(1 for e in log if e["action"] == "ingest") == 1


def test_malformed_json_with_valid_signature_is_rejected_safely(client):
    c, _ = client
    body = b"{not valid json"
    resp = c.post(
        "/webhooks/razorpay/disputes", content=body,
        headers={"X-Razorpay-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_unrecognised_event_is_acked_but_not_processed(client):
    c, db_path = client
    envelope = {**SAMPLE_ENVELOPE, "event": "payment.captured"}
    body = json.dumps(envelope).encode()
    resp = c.post(
        "/webhooks/razorpay/disputes", content=body,
        headers={"X-Razorpay-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert database.get_case(db_path, "disp_EsIAlDcoUr8CaQ") is None


def test_valid_signature_but_schema_violation_is_rejected_not_guessed(client):
    """A structurally-signed payload with an out-of-spec status must fail
    loudly, never be silently coerced into a guessed value."""
    c, db_path = client
    bad = json.loads(json.dumps(SAMPLE_ENVELOPE))
    bad["payload"]["dispute"]["entity"]["status"] = "not_a_real_status"
    body = json.dumps(bad).encode()
    resp = c.post(
        "/webhooks/razorpay/disputes", content=body,
        headers={"X-Razorpay-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert database.get_case(db_path, "disp_EsIAlDcoUr8CaQ") is None
