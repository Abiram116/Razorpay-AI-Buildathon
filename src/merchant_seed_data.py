"""Synthetic chargeback investigation scenarios - merchant.db seed data.

Every order/shipment/communication/refund/document below is fabricated for
this demo. It is NOT Razorpay data and NOT real customer data. This module
is the single source of truth for "what does my demo merchant's shop
contain" - Phase 4's AI investigator, Phase 6's dashboard, and any future
evaluation harness should all be able to point at these same order ids.

`SCENARIOS` deliberately spans both defensible and indefensible cases (see
the mega-spec's Section 6/7 worked examples, both reproduced here as
ORD-1001 and ORD-1002) plus several less clear-cut ones, because the whole
point of Phase 4 is to prove the AI does NOT just default to defending the
merchant. `expected_strength` is a Python-only annotation for my own
development sanity-checking - it is never written to the database and must
never be passed to the AI investigator; that would defeat the entire premise
of an investigation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .dispute_schema import derive_simulated_id
from .merchant_db import Communication, EvidenceDocument, Order, Policy, Refund, Shipment

DAY = 24 * 3600


@dataclass(frozen=True)
class Scenario:
    order: Order
    shipment: Shipment | None
    communications: list[Communication]
    refund: Refund | None
    documents: list[EvidenceDocument]
    # Dev-only label for my own testing; never persisted, never fed to the AI.
    expected_strength: str
    # Paired with the merchant evidence when a matching simulated dispute is
    # ingested via scripts/seed_merchant_db.py --with-cases (Phase 2 reuse).
    dispute_reason_code: str
    dispute_reason_description: str
    # UPI is the dominant payment rail in Indian e-commerce and has its own
    # characteristic dispute pattern (see ORD-1008) - defaults to "card" so
    # existing scenarios are unaffected.
    dispute_payment_method: str = "card"


def _order_ids(merchant_order_id: str) -> tuple[str, str]:
    """Deterministic per merchant order, so "ORD-1001" names the same
    razorpay_order_id/payment_id in every process and on every run."""
    return (
        derive_simulated_id("order", merchant_order_id),
        derive_simulated_id("pay", merchant_order_id),
    )


def _build_scenarios(now: int) -> list[Scenario]:
    scenarios: list[Scenario] = []

    # ------------------------------------------------------------------
    # ORD-1001 - STRONG_CASE. The mega-spec's flagship worked example:
    # signed delivery + the customer's own message admitting receipt.
    # ------------------------------------------------------------------
    razorpay_order_id, payment_id = _order_ids("ORD-1001")
    delivered_at = now - 6 * DAY
    scenarios.append(Scenario(
        order=Order(
            merchant_order_id="ORD-1001", razorpay_order_id=razorpay_order_id,
            payment_id=payment_id, customer_id="CUST-1001", product="Smartphone (128GB)",
            product_type="physical", amount=1_500_000, currency="INR",
            order_timestamp=now - 10 * DAY, order_status="fulfilled",
            shipping_address="Flat 4B, Lotus Apartments, MG Road, Bengaluru 560001",
            billing_address="Flat 4B, Lotus Apartments, MG Road, Bengaluru 560001",
            is_simulated=True,
        ),
        shipment=Shipment(
            merchant_order_id="ORD-1001", tracking_id="DEL1234567",
            courier="BlueDart", shipped_at=now - 9 * DAY, delivered_at=delivered_at,
            delivery_status="delivered", delivery_location="Bengaluru 560001",
            recipient_confirmation="Signed by recipient (OTP verified on delivery)",
        ),
        communications=[
            Communication("ORD-1001", "CUST-1001", now - 9 * DAY, "email",
                           "Your order has shipped! Tracking: DEL1234567", "outbound"),
            Communication("ORD-1001", "CUST-1001", delivered_at + 3600, "chat",
                           "Hey, I received the package today, thank you!", "inbound"),
        ],
        refund=Refund("ORD-1001", payment_id, False, "none", None, None, None),
        documents=[
            EvidenceDocument("ORD-1001", "shipping_proof",
                              "ord-1001-courier-pod.txt",
                              "Courier proof-of-delivery record for tracking DEL1234567: "
                              "delivered and signed for at the shipping address."),
            EvidenceDocument("ORD-1001", "customer_communication",
                              "ord-1001-chat-transcript.txt",
                              "Support chat transcript in which the customer confirms "
                              "receipt of the package."),
        ],
        expected_strength="STRONG_CASE",
        dispute_reason_code="goods_services_not_provided",
        dispute_reason_description="Product not received",
    ))

    # ------------------------------------------------------------------
    # ORD-1002 - NO_CASE. The mega-spec's second worked example: never
    # shipped, customer complained twice before the dispute, no reply.
    # ------------------------------------------------------------------
    razorpay_order_id, payment_id = _order_ids("ORD-1002")
    scenarios.append(Scenario(
        order=Order(
            merchant_order_id="ORD-1002", razorpay_order_id=razorpay_order_id,
            payment_id=payment_id, customer_id="CUST-1002", product="Bluetooth Speaker",
            product_type="physical", amount=349_900, currency="INR",
            order_timestamp=now - 20 * DAY, order_status="confirmed",
            shipping_address="12 Church Street, Pune 411001",
            billing_address="12 Church Street, Pune 411001",
            is_simulated=True,
        ),
        shipment=Shipment(
            merchant_order_id="ORD-1002", tracking_id=None, courier=None,
            shipped_at=None, delivered_at=None, delivery_status="never_shipped",
            delivery_location=None, recipient_confirmation=None,
        ),
        communications=[
            Communication("ORD-1002", "CUST-1002", now - 12 * DAY, "email",
                           "Hi, it's been over a week and I haven't received any shipping "
                           "update. Where is my order?", "inbound"),
            Communication("ORD-1002", "CUST-1002", now - 8 * DAY, "email",
                           "Still no response. Please tell me where my speaker is or "
                           "refund me.", "inbound"),
        ],
        refund=Refund("ORD-1002", payment_id, True, "none", None, None,
                      "customer requested refund via email, no merchant response on file"),
        documents=[],
        expected_strength="NO_CASE",
        dispute_reason_code="goods_services_not_provided",
        dispute_reason_description="Product never arrived",
    ))

    # ------------------------------------------------------------------
    # ORD-1003 - WEAK_CASE. Deliberately the same shape as the ambiguous
    # case used to benchmark the Groq model in Phase 1 (NOTES.md N-010):
    # courier scan to a hub only, no recipient confirmation.
    # ------------------------------------------------------------------
    razorpay_order_id, payment_id = _order_ids("ORD-1003")
    scenarios.append(Scenario(
        order=Order(
            merchant_order_id="ORD-1003", razorpay_order_id=razorpay_order_id,
            payment_id=payment_id, customer_id="CUST-1003", product="Table Lamp (Ceramic)",
            product_type="physical", amount=189_900, currency="INR",
            order_timestamp=now - 15 * DAY, order_status="fulfilled",
            shipping_address="9 MG Road, Kochi 682016",
            billing_address="9 MG Road, Kochi 682016",
            is_simulated=True,
        ),
        shipment=Shipment(
            merchant_order_id="ORD-1003", tracking_id="DEL9988776",
            courier="Delhivery", shipped_at=now - 13 * DAY, delivered_at=None,
            delivery_status="delivered", delivery_location="Kochi Hub (city facility)",
            recipient_confirmation=None,
        ),
        communications=[
            Communication("ORD-1003", "CUST-1003", now - 9 * DAY, "chat",
                           "I never got my lamp, the tracking just says delivered but "
                           "nothing showed up at my door.", "inbound"),
        ],
        refund=Refund("ORD-1003", payment_id, True, "pending", None, None,
                      "under investigation with courier"),
        documents=[
            EvidenceDocument("ORD-1003", "shipping_proof",
                              "ord-1003-courier-scan-log.txt",
                              "Courier scan log shows arrival at the Kochi city hub only. "
                              "No last-mile delivery scan or recipient signature on file."),
        ],
        expected_strength="WEAK_CASE",
        dispute_reason_code="goods_services_not_provided",
        dispute_reason_description="Product not received",
    ))

    # ------------------------------------------------------------------
    # ORD-1004 - STRONG_CASE, digital product. No shipment record at all
    # (product_type='digital' makes that expected, not suspicious); proof
    # of service is an access log instead.
    # ------------------------------------------------------------------
    razorpay_order_id, payment_id = _order_ids("ORD-1004")
    scenarios.append(Scenario(
        order=Order(
            merchant_order_id="ORD-1004", razorpay_order_id=razorpay_order_id,
            payment_id=payment_id, customer_id="CUST-1004",
            product="Pro Video-Editing Course (annual access)", product_type="digital",
            amount=699_900, currency="INR", order_timestamp=now - 40 * DAY,
            order_status="fulfilled", shipping_address=None, billing_address=None,
            is_simulated=True,
        ),
        shipment=None,
        communications=[
            Communication("ORD-1004", "CUST-1004", now - 25 * DAY, "support_ticket",
                           "How do I download the project files for module 3?", "inbound"),
            Communication("ORD-1004", "CUST-1004", now - 25 * DAY, "support_ticket",
                           "They're under Resources > Module 3 > Downloads.", "outbound"),
        ],
        refund=Refund("ORD-1004", payment_id, False, "none", None, None, None),
        documents=[
            EvidenceDocument("ORD-1004", "access_activity_log",
                              "ord-1004-access-log.txt",
                              "Platform access log: 14 separate logins between "
                              "purchase date and dispute date; module 3 marked complete."),
            EvidenceDocument("ORD-1004", "proof_of_service",
                              "ord-1004-course-completion.txt",
                              "Course platform record showing 60% course completion "
                              "under this account."),
        ],
        expected_strength="STRONG_CASE",
        dispute_reason_code="goods_services_not_provided",
        dispute_reason_description="Service not received / not as described",
    ))

    # ------------------------------------------------------------------
    # ORD-1005 - STRONG_CASE. Merchant already resolved this: full refund
    # was processed before the dispute was raised.
    # ------------------------------------------------------------------
    razorpay_order_id, payment_id = _order_ids("ORD-1005")
    refund_ts = now - 5 * DAY
    scenarios.append(Scenario(
        order=Order(
            merchant_order_id="ORD-1005", razorpay_order_id=razorpay_order_id,
            payment_id=payment_id, customer_id="CUST-1005", product="Ceramic Dinner Set",
            product_type="physical", amount=459_900, currency="INR",
            order_timestamp=now - 18 * DAY, order_status="refunded",
            shipping_address="88 Anna Salai, Chennai 600002",
            billing_address="88 Anna Salai, Chennai 600002",
            is_simulated=True,
        ),
        shipment=Shipment(
            merchant_order_id="ORD-1005", tracking_id="DEL5544332",
            courier="Ekart", shipped_at=now - 16 * DAY, delivered_at=now - 14 * DAY,
            delivery_status="delivered", delivery_location="Chennai 600002",
            recipient_confirmation="Signed by recipient",
        ),
        communications=[
            Communication("ORD-1005", "CUST-1005", now - 13 * DAY, "email",
                           "Two of the plates arrived cracked, please refund.", "inbound"),
            Communication("ORD-1005", "CUST-1005", now - 12 * DAY, "email",
                           "Sorry to hear that - refund initiated, 4-6 business days.",
                           "outbound"),
        ],
        refund=Refund("ORD-1005", payment_id, True, "processed", 459_900, refund_ts,
                      "damaged in transit, full refund issued"),
        documents=[
            EvidenceDocument("ORD-1005", "refund_confirmation",
                              "ord-1005-refund-receipt.txt",
                              "Refund confirmation: full amount (INR 4,599.00) refunded "
                              "to original payment method, processed before any dispute."),
        ],
        expected_strength="STRONG_CASE",
        dispute_reason_code="credit_not_processed",
        dispute_reason_description="Customer states a refund was never received",
    ))

    # ------------------------------------------------------------------
    # ORD-1006 - WEAK_CASE. A defect was acknowledged and partially
    # refunded; customer disputes the full amount anyway.
    # ------------------------------------------------------------------
    razorpay_order_id, payment_id = _order_ids("ORD-1006")
    scenarios.append(Scenario(
        order=Order(
            merchant_order_id="ORD-1006", razorpay_order_id=razorpay_order_id,
            payment_id=payment_id, customer_id="CUST-1006", product="Wireless Mouse + Pad Combo",
            product_type="physical", amount=129_900, currency="INR",
            order_timestamp=now - 22 * DAY, order_status="fulfilled",
            shipping_address="21 Park Street, Kolkata 700016",
            billing_address="21 Park Street, Kolkata 700016",
            is_simulated=True,
        ),
        shipment=Shipment(
            merchant_order_id="ORD-1006", tracking_id="DEL7766554",
            courier="BlueDart", shipped_at=now - 20 * DAY, delivered_at=now - 18 * DAY,
            delivery_status="delivered", delivery_location="Kolkata 700016",
            recipient_confirmation="Signed by recipient",
        ),
        communications=[
            Communication("ORD-1006", "CUST-1006", now - 16 * DAY, "chat",
                           "The mouse pad came with a big stain, only the mouse works fine.",
                           "inbound"),
            Communication("ORD-1006", "CUST-1006", now - 15 * DAY, "chat",
                           "We can't send a replacement pad in stock, so we've refunded "
                           "30% of the order for the damaged item.", "outbound"),
        ],
        refund=Refund("ORD-1006", payment_id, True, "processed", 38_970, now - 14 * DAY,
                      "partial refund for damaged mouse pad component"),
        documents=[
            EvidenceDocument("ORD-1006", "refund_confirmation",
                              "ord-1006-partial-refund.txt",
                              "Partial refund of INR 389.70 (30% of order) processed for "
                              "the damaged mouse pad component."),
        ],
        expected_strength="WEAK_CASE",
        dispute_reason_code="goods_services_not_as_described",
        dispute_reason_description="Item received damaged/not as described",
    ))

    # ------------------------------------------------------------------
    # ORD-1007 - NO_CASE. Merchant's own records show the customer
    # cancelled before the charge that's now being disputed.
    # ------------------------------------------------------------------
    razorpay_order_id, payment_id = _order_ids("ORD-1007")
    cancel_ts = now - 30 * DAY
    scenarios.append(Scenario(
        order=Order(
            merchant_order_id="ORD-1007", razorpay_order_id=razorpay_order_id,
            payment_id=payment_id, customer_id="CUST-1007",
            product="Cloud Backup Pro (monthly subscription)", product_type="digital",
            amount=59_900, currency="INR", order_timestamp=now - 25 * DAY,
            order_status="confirmed", shipping_address=None, billing_address=None,
            is_simulated=True,
        ),
        shipment=None,
        communications=[
            Communication("ORD-1007", "CUST-1007", cancel_ts, "support_ticket",
                           "Please cancel my subscription, I don't need it anymore.",
                           "inbound"),
            Communication("ORD-1007", "CUST-1007", cancel_ts + 1800, "support_ticket",
                           "Done, your subscription is cancelled effective immediately.",
                           "outbound"),
            Communication("ORD-1007", "CUST-1007", now - 26 * DAY, "email",
                           "I was charged again after cancelling last month, please explain.",
                           "inbound"),
        ],
        refund=Refund("ORD-1007", payment_id, True, "none", None, None,
                      "customer requested refund for post-cancellation charge; unresolved"),
        documents=[
            EvidenceDocument("ORD-1007", "explanation_letter",
                              "ord-1007-billing-review-note.txt",
                              "Internal billing review: support ticket confirms the "
                              "subscription was cancelled on the customer's request, but "
                              "this order's charge timestamp is AFTER that cancellation - "
                              "the merchant's own records do not support the charge."),
        ],
        expected_strength="NO_CASE",
        dispute_reason_code="subscription_canceled_but_charged",
        dispute_reason_description="Charged after cancelling subscription",
    ))

    # ------------------------------------------------------------------
    # ORD-1008 - STRONG_CASE. The single most common dispute pattern on
    # Indian payment rails: a UPI app shows a transaction as "failed" or
    # stuck "pending" due to a bank-side timeout, even though the debit
    # actually succeeded - the customer disputes a payment that genuinely
    # went through. Razorpay's own payment record (captured=True) plus
    # fulfilment is the merchant's defence; no shipment ambiguity involved.
    # ------------------------------------------------------------------
    razorpay_order_id, payment_id = _order_ids("ORD-1008")
    scenarios.append(Scenario(
        order=Order(
            merchant_order_id="ORD-1008", razorpay_order_id=razorpay_order_id,
            payment_id=payment_id, customer_id="CUST-1008",
            product="Weekly grocery essentials pack", product_type="physical",
            amount=84_900, currency="INR", order_timestamp=now - 4 * DAY,
            order_status="fulfilled",
            shipping_address="B-204 Sunrise Apartments, Whitefield, Bengaluru 560066",
            billing_address="B-204 Sunrise Apartments, Whitefield, Bengaluru 560066",
            is_simulated=True,
        ),
        shipment=Shipment(
            merchant_order_id="ORD-1008", tracking_id="OWN-RIDER-88213",
            courier="Own rider fleet", shipped_at=now - 4 * DAY + 1200,
            delivered_at=now - 4 * DAY + 2100, delivery_status="delivered",
            delivery_location="Whitefield, Bengaluru 560066",
            recipient_confirmation="Delivery OTP 4821 verified by rider app at drop-off",
        ),
        communications=[
            Communication("ORD-1008", "CUST-1008", now - 4 * DAY + 2400, "chat",
                           "My UPI app showed this payment as failed, but you've charged "
                           "me and I never got a confirmation. What happened?", "inbound"),
            Communication("ORD-1008", "CUST-1008", now - 4 * DAY + 3000, "chat",
                           "We can confirm the payment was successfully captured on our "
                           "end (UPI Ref 402873115562) and your order was delivered the "
                           "same day - OTP verified at your address.", "outbound"),
        ],
        refund=Refund("ORD-1008", payment_id, False, "none", None, None, None),
        documents=[
            EvidenceDocument("ORD-1008", "billing_proof",
                              "ord-1008-payment-capture-record.txt",
                              "Razorpay payment record: status=captured, UPI Ref "
                              "402873115562, amount INR 849.00 - the debit that the "
                              "customer's banking app displayed as failed in fact succeeded."),
        ],
        expected_strength="STRONG_CASE",
        dispute_reason_code="unrecognized_transaction",
        dispute_reason_description="Customer says the UPI payment failed / doesn't recognise the charge",
        dispute_payment_method="upi",
    ))

    # ------------------------------------------------------------------
    # ORD-1009 - WEAK_CASE. RTO (Return to Origin): the defining operational
    # pain point of Indian D2C logistics. The courier's own NDR (Non-Delivery
    # Report) log shows genuine delivery attempts, so this is NOT a
    # never_shipped case - but the parcel was ultimately never delivered and
    # no refund has been processed, which is the customer's actual complaint.
    # Deliberately ambiguous: the merchant tried, but did not succeed AND has
    # not yet made the customer whole.
    # ------------------------------------------------------------------
    razorpay_order_id, payment_id = _order_ids("ORD-1009")
    scenarios.append(Scenario(
        order=Order(
            merchant_order_id="ORD-1009", razorpay_order_id=razorpay_order_id,
            payment_id=payment_id, customer_id="CUST-1009",
            product="Non-stick Cookware Set (3-piece)", product_type="physical",
            amount=249_900, currency="INR", order_timestamp=now - 19 * DAY,
            order_status="cancelled",
            shipping_address="Near Old Bus Stand, Sivakasi, Virudhunagar 626123",
            billing_address="Near Old Bus Stand, Sivakasi, Virudhunagar 626123",
            is_simulated=True,
        ),
        shipment=Shipment(
            merchant_order_id="ORD-1009", tracking_id="XB88452209IN",
            courier="XpressBees", shipped_at=now - 17 * DAY, delivered_at=None,
            delivery_status="returned_to_sender",
            delivery_location="Sivakasi, Virudhunagar 626123",
            recipient_confirmation=None,
        ),
        communications=[
            Communication("ORD-1009", "CUST-1009", now - 12 * DAY, "sms",
                           "Where is my order? It's been over a week since it shipped.",
                           "inbound"),
            Communication("ORD-1009", "CUST-1009", now - 11 * DAY, "chat",
                           "Checking with the courier - our records show 3 delivery "
                           "attempts on your PIN code with no response. We'll investigate "
                           "and get back to you.", "outbound"),
            Communication("ORD-1009", "CUST-1009", now - 8 * DAY, "chat",
                           "I was home all three days, nobody came. I still don't have "
                           "my order or my money back.", "inbound"),
        ],
        refund=Refund("ORD-1009", payment_id, True, "pending", None, None,
                      "parcel returned to origin (RTO) by courier; refund not yet processed"),
        documents=[
            EvidenceDocument("ORD-1009", "shipping_proof",
                              "ord-1009-courier-ndr-log.txt",
                              "Courier NDR (Non-Delivery Report) log: 3 delivery attempts "
                              "logged on tracking XB88452209IN, each marked 'customer "
                              "unreachable'; parcel returned to origin (RTO) after the "
                              "third attempt. No independent confirmation the customer "
                              "was actually contacted at the door."),
        ],
        expected_strength="WEAK_CASE",
        dispute_reason_code="goods_services_not_provided",
        dispute_reason_description="Product never arrived (courier marked RTO, no refund issued)",
    ))

    return scenarios


_CACHED_SCENARIOS: list[Scenario] | None = None


def get_scenarios() -> list[Scenario]:
    """The stable scenario catalog for this process.

    Memoized deliberately: `_build_scenarios` mints fresh sim_ ids via
    `generate_simulated_id` (random) on every call, so an uncached version
    would silently hand out a DIFFERENT payment_id for "ORD-1001" on each
    call - any code that seeds a case with one call and looks it up with
    another (exactly what the seeding script and later Phase 4/5/6 code will
    do) would then get a spurious cache miss. Caching once per process makes
    "ORD-1001" mean one consistent thing everywhere it's used.
    """
    global _CACHED_SCENARIOS
    if _CACHED_SCENARIOS is None:
        _CACHED_SCENARIOS = _build_scenarios(int(time.time()))
    return _CACHED_SCENARIOS


def get_policies() -> list[Policy]:
    """Store-wide policy documents, current version only, referenced by
    several scenarios above (ORD-1002's refund request, ORD-1006's partial
    refund) so evidence and policy stay consistent with each other."""
    now = int(time.time())
    return [
        Policy(
            policy_type="refund_policy", version="v2", effective_from=now - 200 * DAY,
            content=(
                "Refund Policy (Synthetic Demo Merchant)\n\n"
                "Items may be returned within 7 days of delivery for a full refund, "
                "provided the item is unused and in its original packaging. Items "
                "damaged in transit are eligible for a full or partial refund at our "
                "discretion, depending on the extent of damage, without requiring "
                "return of the item. Digital products and subscription services are "
                "non-refundable once accessed, except where required by law, but a "
                "subscription cancelled by the customer will not be billed for any "
                "period after the cancellation request is confirmed."
            ),
        ),
        Policy(
            policy_type="cancellation_policy", version="v1", effective_from=now - 300 * DAY,
            content=(
                "Cancellation Policy (Synthetic Demo Merchant)\n\n"
                "Orders can be cancelled free of charge any time before they are "
                "marked as shipped. Subscription services can be cancelled at any "
                "time via support chat or ticket; cancellation takes effect "
                "immediately and stops all future billing for that subscription."
            ),
        ),
        Policy(
            policy_type="terms_and_conditions", version="v1", effective_from=now - 300 * DAY,
            content=(
                "Terms and Conditions (Synthetic Demo Merchant)\n\n"
                "By placing an order, the customer agrees to provide an accurate "
                "delivery address and to be reasonably available to receive the "
                "shipment. Risk of loss for physical goods passes to the customer "
                "upon signed delivery confirmation by the assigned courier."
            ),
        ),
    ]
