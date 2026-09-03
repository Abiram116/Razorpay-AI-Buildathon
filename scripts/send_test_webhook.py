"""Send a locally-signed dispute webhook to a running server, for local proof
that the HMAC verification + parsing + idempotency pipeline actually works.

Signs with the RAZORPAY_WEBHOOK_SECRET from .env, using the exact algorithm
Razorpay documents (HMAC-SHA256, secret as key, raw body as message, hex
digest) - the same computation src.razorpay_client.verify_webhook_signature
performs via the official SDK on the receiving end. This does not simulate a
Razorpay delivery in content; the envelope shape is the verified sample from
https://razorpay.com/docs/webhooks/disputes/, only the id/amount fields are
demo values (and clearly not real Razorpay ids - see README data provenance).

Usage:
    uv run uvicorn src.webhook_handler:app --port 8000 &
    uv run scripts/send_test_webhook.py
    uv run scripts/send_test_webhook.py --bad-signature   # prove rejection
    uv run scripts/send_test_webhook.py --duplicate       # send twice
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from src.config import load_settings  # noqa: E402

URL = "https://oncoming-playgroup-vision.ngrok-free.dev/webhooks/razorpay/disputes"


def build_envelope() -> dict:
    now = int(time.time())
    return {
        "entity": "event",
        "account_id": "acc_LOCALTEST0001",
        "event": "payment.dispute.created",
        "contains": ["payment", "dispute"],
        "payload": {
            "payment": {"entity": {
                "id": "pay_LOCALTEST0001A", "entity": "payment", "amount": 2500000,
                "currency": "INR", "status": "captured", "order_id": "order_LOCALTEST0002B",
                "international": False, "method": "card", "amount_refunded": 0,
                "refund_status": None, "captured": True, "created_at": now - 7 * 24 * 3600,
            }},
            "dispute": {"entity": {
                "id": "disp_LOCALTEST0003C", "entity": "dispute", "payment_id": "pay_LOCALTEST0001A",
                "amount": 2500000, "currency": "INR", "amount_deducted": 0,
                "reason_code": "goods_services_not_provided", "respond_by": now + 5 * 24 * 3600,
                "status": "open", "phase": "chargeback", "created_at": now,
            }},
        },
        "created_at": now,
    }


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main() -> int:
    settings = load_settings(require_razorpay=True)
    secret = settings.razorpay.webhook_secret
    if not secret:
        print("RAZORPAY_WEBHOOK_SECRET is not set in .env - cannot sign a request.")
        return 1

    body = json.dumps(build_envelope()).encode()
    signature = sign(body, secret if "--bad-signature" not in sys.argv else "wrong_secret")

    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature,
        # Bypasses ngrok's free-tier browser interstitial page so the raw
        # POST reaches my FastAPI app instead of an HTML warning page.
        "ngrok-skip-browser-warning": "true",
    }
    resp = requests.post(URL, data=body, headers=headers, timeout=10)
    print(f"POST {URL} -> HTTP {resp.status_code}")
    print(resp.json())

    if "--duplicate" in sys.argv:
        resp2 = requests.post(URL, data=body, headers=headers, timeout=10)
        print(f"\nsecond delivery (same body) -> HTTP {resp2.status_code}")
        print(resp2.json())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
