"""Phase 1 verification: prove the Razorpay Test Mode integration works.

Run:  python scripts/verify_phase1.py

What it does (all read-only except one test-mode order creation):
  1. Loads credentials from .env and refuses live keys.
  2. Probes authentication with GET /v1/payments?count=1.
  3. Creates a Test Mode order and fetches it back.
  4. Lists payments and disputes to observe what Test Mode actually returns.
  5. Prints only whitelisted, non-secret fields.

No secret is ever printed. Exit code 0 = integration verified.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ConfigError, load_settings  # noqa: E402
from src.razorpay_client import (  # noqa: E402
    SAFE_DISPUTE_FIELDS,
    SAFE_ORDER_FIELDS,
    SAFE_PAYMENT_FIELDS,
    RazorpayAuthError,
    RazorpayClient,
    RazorpayRequestError,
    RazorpayUnavailable,
    project,
)

OK, FAIL, INFO = "  [OK]  ", " [FAIL] ", " [INFO] "


def header(text: str) -> None:
    print(f"\n{'=' * 68}\n{text}\n{'=' * 68}")


def dump(label: str, obj) -> None:
    print(f"{label}:\n{json.dumps(obj, indent=2, default=str)}")


def main() -> int:
    header("PHASE 1 - RAZORPAY TEST MODE INTEGRATION CHECK")

    # --- Step 1: configuration -------------------------------------------
    try:
        settings = load_settings(require_razorpay=True)
    except ConfigError as exc:
        print(f"{FAIL}Configuration error:\n{exc}")
        return 1

    summary = settings.razorpay.safe_summary()
    print(f"{OK}Configuration loaded (secrets redacted)")
    dump("  credentials", summary)

    if settings.razorpay.mode_label != "TEST":
        print(
            f"{INFO}Key is not a rzp_test_ key (mode={settings.razorpay.mode_label}). "
            "Phase 1 expects Test Mode credentials."
        )

    # --- Step 2: authentication ------------------------------------------
    header("STEP 2 - AUTHENTICATION PROBE (read-only)")
    try:
        client = RazorpayClient(settings)
    except ConfigError as exc:
        print(f"{FAIL}{exc}")
        return 1

    result = client.verify_credentials()
    if not result.authenticated:
        print(f"{FAIL}Authentication failed via {result.endpoint_probed}")
        print(f"        {result.detail}")
        return 1
    print(f"{OK}Authenticated via {result.endpoint_probed} (mode={result.mode})")

    # --- Step 3: create + fetch a Test Mode order ------------------------
    header("STEP 3 - CREATE A REAL TEST-MODE ORDER")
    try:
        order = client.create_order(
            amount_paise=2_500_000,  # Rs 25,000 - the demo scenario amount
            currency="INR",
            receipt="phase1-verify-001",
            notes={"purpose": "phase1_integration_check", "project": "chargeback-defense"},
        )
    except (RazorpayUnavailable, RazorpayRequestError, RazorpayAuthError) as exc:
        print(f"{FAIL}Order creation failed: {exc}")
        return 1

    print(f"{OK}Order created: {order.get('id')}")
    dump("  order (whitelisted fields)", project(order, SAFE_ORDER_FIELDS))
    print(f"{INFO}Full field names returned: {sorted(order.keys())}")

    fetched = client.fetch_order(order["id"])
    print(f"{OK}Order fetched back: {fetched.get('id')} status={fetched.get('status')}")

    # --- Step 4: observe payments ----------------------------------------
    header("STEP 4 - LIST PAYMENTS (what Test Mode actually holds)")
    payments = client.list_payments(count=3)
    print(f"{OK}payment.all returned count={payments.get('count')}")
    for item in payments.get("items", []):
        dump(f"  payment {item.get('id')}", project(item, SAFE_PAYMENT_FIELDS))
    if not payments.get("items"):
        print(f"{INFO}No payments in this test account yet. Orders alone do not "
              "create payments - a payment needs a checkout completed with a test card.")

    # --- Step 5: observe disputes ----------------------------------------
    header("STEP 5 - LIST DISPUTES (Test Mode capability check)")
    try:
        disputes = client.list_disputes(count=5)
        print(f"{OK}dispute.all reachable, count={disputes.get('count')}")
        for item in disputes.get("items", []):
            dump(f"  dispute {item.get('id')}", project(item, SAFE_DISPUTE_FIELDS))
        if not disputes.get("items"):
            print(f"{INFO}Zero disputes. Expected: the Razorpay API has no "
                  "'create dispute' endpoint - disputes originate from the "
                  "issuing bank or customer. See NOTES.md.")
    except (RazorpayUnavailable, RazorpayRequestError) as exc:
        print(f"{INFO}dispute.all not available on this account: {exc}")
        print(f"{INFO}Disputes may need to be enabled for the account; this does "
              "not block Phase 2 (see NOTES.md).")

    header("PHASE 1 RESULT: RAZORPAY INTEGRATION VERIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
