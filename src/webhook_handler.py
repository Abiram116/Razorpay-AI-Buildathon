"""FastAPI receiver for Razorpay's payment.dispute.* webhooks.

Run: uv run uvicorn src.webhook_handler:app --port 8000
Route: POST /webhooks/razorpay/disputes

Security-critical ordering, do not reorder:
  1. Read the RAW request body (bytes, unparsed).
  2. Verify X-Razorpay-Signature over those exact raw bytes.
  3. Only THEN parse JSON and act on it.
Parsing before verifying would let an attacker who cannot forge a signature
still get arbitrary JSON parsed and logged by this service.
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import database
from .config import ConfigError, Settings, load_settings
from .dispute_schema import DISPUTE_EVENTS, DisputeValidationError, parse_webhook_envelope
from .ingestion import ingest_dispute
from .razorpay_client import RazorpayClient

logger = logging.getLogger("webhook_handler")

app = FastAPI(title="AI Chargeback Defense Agent - Webhook Receiver")

_settings: Settings | None = None
_client: RazorpayClient | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings(require_razorpay=True)
        database.init_case_db(_settings.paths.case_db)
    return _settings


def _get_client() -> RazorpayClient:
    global _client
    if _client is None:
        _client = RazorpayClient(_get_settings())
    return _client


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/webhooks/razorpay/disputes")
async def receive_dispute_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(default=None),
) -> JSONResponse:
    raw_body: bytes = await request.body()

    try:
        settings = _get_settings()
        client = _get_client()
        # CREATE TABLE IF NOT EXISTS is idempotent and cheap; running it on
        # every request means the server self-heals if its DB file is ever
        # deleted or replaced out from under it, instead of 500ing on every
        # request until someone notices and restarts the process.
        database.init_case_db(settings.paths.case_db)
    except ConfigError as exc:
        # My own misconfiguration, not the caller's fault. 500 is correct
        # here: Razorpay should retry once it's fixed.
        logger.error("webhook received but server is misconfigured: %s", exc)
        raise HTTPException(status_code=500, detail="server misconfigured") from exc

    if not x_razorpay_signature:
        logger.warning("webhook rejected: missing X-Razorpay-Signature header")
        raise HTTPException(status_code=400, detail="missing signature header")

    try:
        signature_valid = client.verify_webhook_signature(
            raw_body.decode("utf-8"), x_razorpay_signature
        )
    except ConfigError as exc:
        logger.error("webhook received but RAZORPAY_WEBHOOK_SECRET is not set: %s", exc)
        raise HTTPException(status_code=500, detail="webhook secret not configured") from exc

    body_hash = database.hash_webhook_body(raw_body)

    if not signature_valid:
        # Record the attempt (signature_valid=False) for security visibility,
        # but never process the payload. Never echo back what I computed.
        database.record_webhook_receipt(
            settings.paths.case_db, body_hash, event_type="unknown",
            dispute_id=None, signature_valid=False,
        )
        logger.warning("webhook rejected: invalid signature (body_hash=%s)", body_hash[:12])
        raise HTTPException(status_code=400, detail="invalid signature")

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        logger.warning("webhook rejected: body is not valid JSON despite valid signature")
        raise HTTPException(status_code=400, detail="malformed JSON body") from exc

    event_name = body.get("event", "unknown")

    # Idempotency: an identical delivery (Razorpay retries on non-2xx, or an
    # operator replay) must not be processed twice. Keyed on raw-body hash
    # because the envelope carries no delivery/event id (verified against the
    # documented payload - NOTES.md N-006 / BUILD_LOG 2026-09-02-01).
    is_new = database.record_webhook_receipt(
        settings.paths.case_db, body_hash, event_type=event_name,
        dispute_id=None, signature_valid=True,
    )
    if not is_new:
        logger.info("duplicate webhook delivery ignored (body_hash=%s)", body_hash[:12])
        return JSONResponse({"status": "duplicate_ignored"}, status_code=200)

    if event_name not in DISPUTE_EVENTS:
        # Ack with 200 so Razorpay does not retry an event type I simply
        # don't subscribe to processing logic for - only 4-6 dispute events
        # are wired here (see dispute_schema.DISPUTE_EVENTS).
        logger.info("webhook acked but not processed: unrecognised event %r", event_name)
        return JSONResponse({"status": "ignored", "event": event_name}, status_code=200)

    try:
        _, dispute, payment = parse_webhook_envelope(body)
    except DisputeValidationError as exc:
        # Signature was valid, so this really did come from Razorpay - but I
        # cannot safely act on it. Fail loudly rather than guessing at fields.
        logger.error("webhook payload failed validation for event=%s: %s", event_name, exc)
        raise HTTPException(status_code=400, detail=f"payload validation failed: {exc}") from exc

    record = ingest_dispute(
        settings.paths.case_db, dispute, payment, source="razorpay_webhook", actor="razorpay_webhook",
    )
    database.mark_webhook_processed(settings.paths.case_db, body_hash)

    logger.info(
        "webhook processed: event=%s dispute_id=%s case_state=%s",
        event_name, dispute.id, record.case_state,
    )
    return JSONResponse(
        {"status": "processed", "event": event_name, "dispute_id": dispute.id,
         "case_state": record.case_state},
        status_code=200,
    )
