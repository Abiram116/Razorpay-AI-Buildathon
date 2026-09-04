"""The held-out evaluation set: synthetic cases with hidden ground truth.

Built entirely in Python, on purpose. Two reasons I didn't have an LLM write
these:

1. Generating 200 cases through Groq would burn the exact rate-limit budget
   the evaluation itself needs.
2. If a model writes the case AND a model grades it, the ground truth is only
   as trustworthy as the generator. Here I plant the facts and the label
   together - the label is derived from what I put in the case, not inferred
   from it afterwards. That's the whole point of a held-out set.

Every archetype below is a dispute pattern Indian merchants actually deal
with: UPI showing "failed" on a payment that captured, RTO after failed NDR
attempts, hub-scan-only deliveries, subscriptions billed after cancellation.
Variation (city, courier, product, amount, dates, payment rail) is seeded
deterministically, so the same dataset_version always produces byte-identical
cases - otherwise a re-run would be measuring a different thing.

GROUND TRUTH (spec section 17):
  DEFENSIBLE   - the merchant has enough evidence that contesting is reasonable
  INDEFENSIBLE - the merchant cannot show it met its obligation; don't contest

`ground_truth` must never reach the model. Only the case facts do.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Literal

from .database import CaseRecord
from .dispute_schema import derive_simulated_id
from .merchant_db import (
    CaseEvidence,
    Communication,
    EvidenceDocument,
    Order,
    Policy,
    Refund,
    Shipment,
)

DAY = 24 * 3600
GroundTruth = Literal["DEFENSIBLE", "INDEFENSIBLE"]
Split = Literal["dev", "holdout"]

DATASET_VERSION = "v1"

# Fixed reference time so a regenerated dataset is identical across runs.
# (Real timestamps would make every run a different dataset.)
_EPOCH = 1_780_000_000

_CITIES = [
    ("Bengaluru", "560001"), ("Pune", "411001"), ("Kochi", "682016"),
    ("Chennai", "600002"), ("Kolkata", "700016"), ("Hyderabad", "500032"),
    ("Jaipur", "302001"), ("Lucknow", "226001"), ("Indore", "452001"),
    ("Surat", "395003"), ("Coimbatore", "641012"), ("Guwahati", "781005"),
    ("Sivakasi", "626123"), ("Nagpur", "440010"), ("Bhubaneswar", "751024"),
    ("Patna", "800001"), ("Raipur", "492001"), ("Madurai", "625001"),
]
_COURIERS = ["BlueDart", "Delhivery", "Ekart", "XpressBees", "DTDC",
             "Shadowfax", "Ecom Express"]
_PHYSICAL_PRODUCTS = [
    ("Smartphone (128GB)", 1_499_900), ("Bluetooth Speaker", 349_900),
    ("Ceramic Dinner Set", 459_900), ("Non-stick Cookware Set", 249_900),
    ("Wireless Earbuds", 199_900), ("Cotton Bedsheet Set", 129_900),
    ("Running Shoes", 279_900), ("Table Lamp (Ceramic)", 189_900),
    ("Kurta Set", 159_900), ("Steel Water Bottle (2L)", 74_900),
    ("Laptop Backpack", 219_900), ("Air Fryer 4L", 699_900),
    ("Weekly grocery essentials pack", 84_900), ("Face Serum 30ml", 94_900),
    ("Cricket Bat (English Willow)", 899_900), ("Study Table Lamp", 139_900),
]
_DIGITAL_PRODUCTS = [
    ("Pro Video-Editing Course (annual)", 699_900),
    ("Cloud Backup Pro (monthly)", 59_900),
    ("UPSC Prep Test Series", 449_900),
    ("Spoken English Course (6 months)", 349_900),
    ("Design Tool Subscription (annual)", 899_900),
    ("Stock Market Basics Course", 199_900),
]
_UPI_APPS = ["PhonePe", "Google Pay", "Paytm", "BHIM"]
_PAYMENT_METHODS_PHYSICAL = ["upi", "upi", "upi", "card", "netbanking", "wallet"]


@dataclass(frozen=True)
class EvaluationCase:
    """One labelled case. `ground_truth` is never shown to the model."""

    case_id: str
    archetype: str
    ground_truth: GroundTruth
    split: Split
    case: CaseRecord
    evidence: CaseEvidence
    # Why this label is correct - for a human auditing the dataset, never sent
    # to the model.
    label_rationale: str


def _policies(now: int) -> list[Policy]:
    return [
        Policy("refund_policy", "v2", now - 200 * DAY,
               "Items may be returned within 7 days of delivery for a full refund if "
               "unused and in original packaging. Items damaged in transit are eligible "
               "for a full or partial refund at our discretion. Digital products and "
               "subscriptions are non-refundable once accessed, except where required "
               "by law; a subscription cancelled by the customer is not billed for any "
               "period after the cancellation is confirmed."),
        Policy("cancellation_policy", "v1", now - 300 * DAY,
               "Orders can be cancelled free of charge any time before they are marked "
               "shipped. Subscriptions can be cancelled any time via support; "
               "cancellation takes effect immediately and stops all future billing."),
        Policy("terms_and_conditions", "v1", now - 300 * DAY,
               "Customers agree to provide an accurate delivery address and to be "
               "reasonably available to receive the shipment. Risk of loss for physical "
               "goods passes to the customer upon signed delivery confirmation by the "
               "assigned courier."),
    ]


def _build_case_record(
    case_id: str, amount: int, reason_code: str, now: int, rng: random.Random
) -> CaseRecord:
    return CaseRecord(
        dispute_id=derive_simulated_id("disp", case_id),
        payment_id=derive_simulated_id("pay", case_id),
        order_id=derive_simulated_id("order", case_id),
        amount=amount,
        currency="INR",
        reason_code=reason_code,
        respond_by=now + rng.randint(12, 14 * 24) * 3600,
        dispute_status="open",
        phase="chargeback",
        case_state="INGESTED",
        source="simulated",
        is_simulated=True,
        ingested_at=now,
    )


# ----------------------------------------------------------------------
# archetypes
#
# Each returns (evidence, ground_truth, reason_code, rationale). The label
# follows from the facts the archetype plants - not from a guess about what
# a model might say.
# ----------------------------------------------------------------------

def _arch_upi_captured_but_claimed_failed(oid, now, rng):
    """UPI app showed 'failed' / stayed pending; the debit actually went
    through and the order was delivered. Extremely common in India."""
    city, pin = rng.choice(_CITIES)
    product, amount = rng.choice(_PHYSICAL_PRODUCTS)
    app = rng.choice(_UPI_APPS)
    addr = f"{rng.randint(1, 400)} {rng.choice(['MG Road','Main Road','Nehru Nagar','Gandhi Street'])}, {city} {pin}"
    delivered = now - rng.randint(3, 9) * DAY
    order = Order(oid, derive_simulated_id("order", oid), derive_simulated_id("pay", oid),
                  f"CUST-{oid[-4:]}", product, "physical", amount, "INR",
                  delivered - 2 * DAY, "fulfilled", addr, addr, True)
    shipment = Shipment(oid, f"{rng.choice(['BD','DL','XB','EK'])}{rng.randint(10**8, 10**9)}IN",
                        rng.choice(_COURIERS), delivered - DAY, delivered, "delivered",
                        f"{city} {pin}", f"Delivery OTP {rng.randint(1000, 9999)} verified at drop-off")
    comms = [
        Communication(oid, order.customer_id, delivered + 2 * 3600, "chat",
                      f"My {app} app showed this payment as failed but you've charged me. "
                      "I want this reversed.", "inbound", id=1),
        Communication(oid, order.customer_id, delivered + 3 * 3600, "chat",
                      f"The payment was captured successfully on our end (UPI Ref "
                      f"{rng.randint(10**11, 10**12)}) and your order was delivered and "
                      "OTP-verified at your address the same day.", "outbound", id=2),
    ]
    refund = Refund(oid, order.payment_id, False, "none", None, None, None)
    docs = [
        EvidenceDocument(oid, "billing_proof", f"{oid.lower()}-capture.txt",
                         f"Payment record: status=captured via UPI, amount INR {amount/100:,.2f}. "
                         "The debit the customer's app displayed as failed did in fact succeed.", id=1),
        EvidenceDocument(oid, "shipping_proof", f"{oid.lower()}-pod.txt",
                         "Courier proof of delivery with OTP verification at the delivery address.", id=2),
    ]
    return (shipment, comms, refund, docs, order, "DEFENSIBLE", "unrecognized_transaction",
            "Payment captured and order delivered with OTP proof; the customer's claim rests "
            "on their app's display, not on non-delivery.")


def _arch_delivered_and_admitted(oid, now, rng):
    """Signed delivery plus the customer's own message admitting receipt."""
    city, pin = rng.choice(_CITIES)
    product, amount = rng.choice(_PHYSICAL_PRODUCTS)
    addr = f"Flat {rng.randint(1, 40)}{rng.choice('ABCD')}, {rng.choice(['Lotus','Palm','Green','Sunrise'])} Apartments, {city} {pin}"
    delivered = now - rng.randint(4, 12) * DAY
    order = Order(oid, derive_simulated_id("order", oid), derive_simulated_id("pay", oid),
                  f"CUST-{oid[-4:]}", product, "physical", amount, "INR",
                  delivered - 4 * DAY, "fulfilled", addr, addr, True)
    shipment = Shipment(oid, f"{rng.choice(['BD','DL','XB'])}{rng.randint(10**8, 10**9)}IN",
                        rng.choice(_COURIERS), delivered - 3 * DAY, delivered, "delivered",
                        f"{city} {pin}", "Signed by recipient (OTP verified on delivery)")
    comms = [
        Communication(oid, order.customer_id, delivered - 3 * DAY, "email",
                      "Your order has shipped.", "outbound", id=1),
        Communication(oid, order.customer_id, delivered + 4 * 3600, "chat",
                      "Got the package today, thanks!", "inbound", id=2),
    ]
    refund = Refund(oid, order.payment_id, False, "none", None, None, None)
    docs = [
        EvidenceDocument(oid, "shipping_proof", f"{oid.lower()}-pod.txt",
                         "Courier proof of delivery, signed at the shipping address.", id=1),
        EvidenceDocument(oid, "customer_communication", f"{oid.lower()}-chat.txt",
                         "Chat transcript where the customer confirms receipt.", id=2),
    ]
    return (shipment, comms, refund, docs, order, "DEFENSIBLE", "goods_services_not_provided",
            "Signed delivery plus the customer's own written admission of receipt directly "
            "contradicts the non-delivery claim.")


def _arch_digital_access_proven(oid, now, rng):
    """Digital product with heavy logged usage after purchase."""
    product, amount = rng.choice(_DIGITAL_PRODUCTS)
    purchased = now - rng.randint(20, 60) * DAY
    logins = rng.randint(9, 40)
    order = Order(oid, derive_simulated_id("order", oid), derive_simulated_id("pay", oid),
                  f"CUST-{oid[-4:]}", product, "digital", amount, "INR",
                  purchased, "fulfilled", None, None, True)
    comms = [
        Communication(oid, order.customer_id, purchased + 6 * DAY, "support_ticket",
                      "How do I download the module 3 worksheets?", "inbound", id=1),
        Communication(oid, order.customer_id, purchased + 6 * DAY + 3600, "support_ticket",
                      "They're under Resources > Module 3 > Downloads.", "outbound", id=2),
    ]
    refund = Refund(oid, order.payment_id, False, "none", None, None, None)
    docs = [
        EvidenceDocument(oid, "access_activity_log", f"{oid.lower()}-access.txt",
                         f"Platform access log: {logins} logins between purchase and dispute "
                         f"date from this account.", id=1),
        EvidenceDocument(oid, "proof_of_service", f"{oid.lower()}-progress.txt",
                         f"Course progress record: {rng.randint(35, 90)}% completed.", id=2),
    ]
    return (None, comms, refund, docs, order, "DEFENSIBLE", "goods_services_not_provided",
            "Access logs and progress records show the digital product was delivered and "
            "actively used; there is no shipment because none is applicable.")


def _arch_refund_already_processed(oid, now, rng):
    """Merchant already refunded in full before the dispute was raised."""
    city, pin = rng.choice(_CITIES)
    product, amount = rng.choice(_PHYSICAL_PRODUCTS)
    addr = f"{rng.randint(1, 200)} {rng.choice(['Park Street','Anna Salai','Church Road'])}, {city} {pin}"
    delivered = now - rng.randint(14, 25) * DAY
    refunded = delivered + 4 * DAY
    order = Order(oid, derive_simulated_id("order", oid), derive_simulated_id("pay", oid),
                  f"CUST-{oid[-4:]}", product, "physical", amount, "INR",
                  delivered - 3 * DAY, "refunded", addr, addr, True)
    shipment = Shipment(oid, f"EK{rng.randint(10**8, 10**9)}IN", rng.choice(_COURIERS),
                        delivered - 2 * DAY, delivered, "delivered", f"{city} {pin}",
                        "Signed by recipient")
    comms = [
        Communication(oid, order.customer_id, delivered + DAY, "email",
                      "Item arrived damaged, I want a refund.", "inbound", id=1),
        Communication(oid, order.customer_id, delivered + 2 * DAY, "email",
                      "Sorry about that - full refund initiated, 4-6 business days.",
                      "outbound", id=2),
    ]
    refund = Refund(oid, order.payment_id, True, "processed", amount, refunded,
                    "damaged in transit, full refund issued before any dispute")
    docs = [
        EvidenceDocument(oid, "refund_confirmation", f"{oid.lower()}-refund.txt",
                         f"Refund confirmation: full amount INR {amount/100:,.2f} returned to "
                         "the original payment method before the dispute was raised.", id=1),
    ]
    return (shipment, comms, refund, docs, order, "DEFENSIBLE", "credit_not_processed",
            "The refund the customer says they never received was processed in full, before "
            "the dispute existed.")


def _arch_never_shipped_ignored(oid, now, rng):
    """Never shipped, customer chased repeatedly, merchant never replied."""
    city, pin = rng.choice(_CITIES)
    product, amount = rng.choice(_PHYSICAL_PRODUCTS)
    addr = f"{rng.randint(1, 90)} {rng.choice(['Church Street','Station Road','Bazaar Road'])}, {city} {pin}"
    ordered = now - rng.randint(18, 30) * DAY
    order = Order(oid, derive_simulated_id("order", oid), derive_simulated_id("pay", oid),
                  f"CUST-{oid[-4:]}", product, "physical", amount, "INR",
                  ordered, "confirmed", addr, addr, True)
    shipment = Shipment(oid, None, None, None, None, "never_shipped", None, None)
    comms = [
        Communication(oid, order.customer_id, ordered + 8 * DAY, "email",
                      "It's been over a week with no shipping update. Where is my order?",
                      "inbound", id=1),
        Communication(oid, order.customer_id, ordered + 12 * DAY, "email",
                      "Still nothing and no reply. Please ship it or refund me.", "inbound", id=2),
    ]
    refund = Refund(oid, order.payment_id, True, "none", None, None,
                    "customer requested refund; no merchant action on file")
    return (shipment, comms, refund, [], order, "INDEFENSIBLE", "goods_services_not_provided",
            "Never shipped, two unanswered complaints, no refund. Nothing supports a contest.")


def _arch_rto_unrefunded(oid, now, rng):
    """RTO after failed NDR attempts, still no refund. Very Indian."""
    city, pin = rng.choice(_CITIES)
    product, amount = rng.choice(_PHYSICAL_PRODUCTS)
    addr = f"Near {rng.choice(['Old Bus Stand','Railway Gate','Market Road'])}, {city} {pin}"
    ordered = now - rng.randint(16, 28) * DAY
    order = Order(oid, derive_simulated_id("order", oid), derive_simulated_id("pay", oid),
                  f"CUST-{oid[-4:]}", product, "physical", amount, "INR",
                  ordered, "cancelled", addr, addr, True)
    courier = rng.choice(_COURIERS)
    shipment = Shipment(oid, f"XB{rng.randint(10**8, 10**9)}IN", courier, ordered + 2 * DAY,
                        None, "returned_to_sender", f"{city} {pin}", None)
    comms = [
        Communication(oid, order.customer_id, ordered + 9 * DAY, "sms",
                      "Where is my order? It shipped over a week ago.", "inbound", id=1),
        Communication(oid, order.customer_id, ordered + 10 * DAY, "chat",
                      f"{courier} logged 3 delivery attempts with no response at your PIN "
                      "code. We're checking with them.", "outbound", id=2),
        Communication(oid, order.customer_id, ordered + 13 * DAY, "chat",
                      "I was home all three days, nobody came. I still have no order and no "
                      "refund.", "inbound", id=3),
    ]
    refund = Refund(oid, order.payment_id, True, "pending", None, None,
                    "parcel returned to origin (RTO); refund not processed")
    docs = [
        EvidenceDocument(oid, "shipping_proof", f"{oid.lower()}-ndr.txt",
                         "Courier NDR log: 3 attempts marked 'customer unreachable', parcel "
                         "returned to origin. No independent confirmation the customer was "
                         "actually contacted.", id=1),
    ]
    return (shipment, comms, refund, docs, order, "INDEFENSIBLE", "goods_services_not_provided",
            "Attempted delivery is not delivery. The goods came back and the customer was "
            "never refunded, so the core complaint stands.")


def _arch_charged_after_cancellation(oid, now, rng):
    """Subscription cancelled, then billed anyway."""
    product, amount = rng.choice(_DIGITAL_PRODUCTS)
    cancelled = now - rng.randint(30, 45) * DAY
    charged = cancelled + rng.randint(4, 12) * DAY
    order = Order(oid, derive_simulated_id("order", oid), derive_simulated_id("pay", oid),
                  f"CUST-{oid[-4:]}", product, "digital", amount, "INR",
                  charged, "confirmed", None, None, True)
    comms = [
        Communication(oid, order.customer_id, cancelled, "support_ticket",
                      "Please cancel my subscription.", "inbound", id=1),
        Communication(oid, order.customer_id, cancelled + 1800, "support_ticket",
                      "Done, cancelled with immediate effect.", "outbound", id=2),
        Communication(oid, order.customer_id, charged + 2 * DAY, "email",
                      "You charged me again after I cancelled. Explain.", "inbound", id=3),
    ]
    refund = Refund(oid, order.payment_id, True, "none", None, None,
                    "customer disputes post-cancellation charge; unresolved")
    docs = [
        EvidenceDocument(oid, "explanation_letter", f"{oid.lower()}-billing-review.txt",
                         "Internal billing review: support confirmed cancellation, and this "
                         "charge is dated after it. Our own records do not support the charge.", id=1),
    ]
    return (None, comms, refund, docs, order, "INDEFENSIBLE", "subscription_canceled_but_charged",
            "The merchant's own support log confirms cancellation before the charge date, and "
            "policy says cancellation stops future billing.")


def _arch_hub_scan_only(oid, now, rng):
    """Courier scanned to city hub, no last-mile scan, no signature."""
    city, pin = rng.choice(_CITIES)
    product, amount = rng.choice(_PHYSICAL_PRODUCTS)
    addr = f"{rng.randint(1, 120)} {rng.choice(['MG Road','Link Road','Ring Road'])}, {city} {pin}"
    ordered = now - rng.randint(12, 22) * DAY
    order = Order(oid, derive_simulated_id("order", oid), derive_simulated_id("pay", oid),
                  f"CUST-{oid[-4:]}", product, "physical", amount, "INR",
                  ordered, "fulfilled", addr, addr, True)
    shipment = Shipment(oid, f"DL{rng.randint(10**8, 10**9)}IN", rng.choice(_COURIERS),
                        ordered + 2 * DAY, None, "delivered", f"{city} Hub (city facility)", None)
    comms = [
        Communication(oid, order.customer_id, ordered + 8 * DAY, "chat",
                      "Tracking says delivered but nothing arrived at my door.", "inbound", id=1),
    ]
    refund = Refund(oid, order.payment_id, True, "pending", None, None,
                    "under investigation with courier")
    docs = [
        EvidenceDocument(oid, "shipping_proof", f"{oid.lower()}-scan.txt",
                         "Courier scan log shows arrival at the city hub only. No last-mile "
                         "delivery scan and no recipient signature on file.", id=1),
    ]
    return (shipment, comms, refund, docs, order, "INDEFENSIBLE", "goods_services_not_provided",
            "delivery_status says delivered but the only evidence is a hub scan - the merchant "
            "cannot show the parcel reached the customer, and its own T&Cs require signed "
            "delivery for risk to transfer.")


def _arch_admitted_defect_unresolved(oid, now, rng):
    """Merchant admitted the item was faulty and promised a refund, never sent it."""
    city, pin = rng.choice(_CITIES)
    product, amount = rng.choice(_PHYSICAL_PRODUCTS)
    addr = f"{rng.randint(1, 150)} {rng.choice(['Model Town','Civil Lines','Sector 12'])}, {city} {pin}"
    delivered = now - rng.randint(15, 26) * DAY
    order = Order(oid, derive_simulated_id("order", oid), derive_simulated_id("pay", oid),
                  f"CUST-{oid[-4:]}", product, "physical", amount, "INR",
                  delivered - 3 * DAY, "fulfilled", addr, addr, True)
    shipment = Shipment(oid, f"BD{rng.randint(10**8, 10**9)}IN", rng.choice(_COURIERS),
                        delivered - 2 * DAY, delivered, "delivered", f"{city} {pin}",
                        "Signed by recipient")
    comms = [
        Communication(oid, order.customer_id, delivered + DAY, "chat",
                      "It arrived broken, doesn't power on at all.", "inbound", id=1),
        Communication(oid, order.customer_id, delivered + 2 * DAY, "chat",
                      "Sorry about that - it's a known defective batch. We'll refund you "
                      "this week.", "outbound", id=2),
        Communication(oid, order.customer_id, delivered + 12 * DAY, "chat",
                      "It's been almost two weeks, still no refund.", "inbound", id=3),
    ]
    refund = Refund(oid, order.payment_id, True, "none", None, None,
                    "refund promised in writing, never processed")
    docs = [
        EvidenceDocument(oid, "customer_communication", f"{oid.lower()}-chat.txt",
                         "Support chat where the merchant acknowledges a defective batch and "
                         "promises a refund that was never issued.", id=1),
    ]
    return (shipment, comms, refund, docs, order, "INDEFENSIBLE", "goods_services_not_as_described",
            "The merchant admitted the defect in writing and promised a refund it never paid. "
            "Delivery proof doesn't answer a 'not as described' claim.")


def _arch_partial_refund_disputed_full(oid, now, rng):
    """Defect on part of the order, partial refund paid, customer disputes the lot."""
    city, pin = rng.choice(_CITIES)
    product, amount = rng.choice(_PHYSICAL_PRODUCTS)
    addr = f"{rng.randint(1, 90)} {rng.choice(['Park Street','Brigade Road','FC Road'])}, {city} {pin}"
    delivered = now - rng.randint(16, 24) * DAY
    partial = int(amount * 0.3)
    order = Order(oid, derive_simulated_id("order", oid), derive_simulated_id("pay", oid),
                  f"CUST-{oid[-4:]}", product, "physical", amount, "INR",
                  delivered - 3 * DAY, "fulfilled", addr, addr, True)
    shipment = Shipment(oid, f"BD{rng.randint(10**8, 10**9)}IN", rng.choice(_COURIERS),
                        delivered - 2 * DAY, delivered, "delivered", f"{city} {pin}",
                        "Signed by recipient")
    comms = [
        Communication(oid, order.customer_id, delivered + 2 * DAY, "chat",
                      "One item in the set was stained.", "inbound", id=1),
        Communication(oid, order.customer_id, delivered + 3 * DAY, "chat",
                      "No replacement stock, so we've refunded 30% for that item.",
                      "outbound", id=2),
    ]
    refund = Refund(oid, order.payment_id, True, "processed", partial, delivered + 4 * DAY,
                    "partial refund for the damaged component")
    docs = [
        EvidenceDocument(oid, "shipping_proof", f"{oid.lower()}-pod.txt",
                         "Signed proof of delivery for the full order.", id=1),
        EvidenceDocument(oid, "refund_confirmation", f"{oid.lower()}-partial.txt",
                         f"Partial refund of INR {partial/100:,.2f} processed for the damaged "
                         "component; the rest of the order was delivered and kept.", id=2),
    ]
    return (shipment, comms, refund, docs, order, "DEFENSIBLE", "goods_services_not_as_described",
            "Delivery is proven, the defect was acknowledged and partially refunded in good "
            "faith, and the customer kept the rest - a full reversal isn't supported.")


def _arch_delivered_late_after_complaint(oid, now, rng):
    """Festive-surge delay: late, complained about, but genuinely delivered."""
    city, pin = rng.choice(_CITIES)
    product, amount = rng.choice(_PHYSICAL_PRODUCTS)
    addr = f"{rng.randint(1, 200)} {rng.choice(['Kalyan Nagar','Salt Lake','Andheri East'])}, {city} {pin}"
    ordered = now - rng.randint(22, 34) * DAY
    delivered = ordered + rng.randint(11, 17) * DAY
    order = Order(oid, derive_simulated_id("order", oid), derive_simulated_id("pay", oid),
                  f"CUST-{oid[-4:]}", product, "physical", amount, "INR",
                  ordered, "fulfilled", addr, addr, True)
    shipment = Shipment(oid, f"DL{rng.randint(10**8, 10**9)}IN", rng.choice(_COURIERS),
                        ordered + 7 * DAY, delivered, "delivered", f"{city} {pin}",
                        "Signed by recipient")
    comms = [
        Communication(oid, order.customer_id, ordered + 6 * DAY, "email",
                      "This was supposed to arrive days ago. Festive rush is not my problem.",
                      "inbound", id=1),
        Communication(oid, order.customer_id, ordered + 7 * DAY, "email",
                      "Apologies - dispatched now, tracking attached.", "outbound", id=2),
    ]
    refund = Refund(oid, order.payment_id, False, "none", None, None, None)
    docs = [
        EvidenceDocument(oid, "shipping_proof", f"{oid.lower()}-pod.txt",
                         "Signed proof of delivery, late but completed.", id=1),
    ]
    return (shipment, comms, refund, docs, order, "DEFENSIBLE", "goods_services_not_provided",
            "The order was late but demonstrably delivered and signed for; lateness is a "
            "service complaint, not non-delivery.")


def _arch_duplicate_charge_confirmed(oid, now, rng):
    """Merchant's own records show two captures for one order."""
    city, pin = rng.choice(_CITIES)
    product, amount = rng.choice(_PHYSICAL_PRODUCTS)
    addr = f"{rng.randint(1, 100)} {rng.choice(['JP Nagar','Banjara Hills','Alkapuri'])}, {city} {pin}"
    delivered = now - rng.randint(10, 20) * DAY
    order = Order(oid, derive_simulated_id("order", oid), derive_simulated_id("pay", oid),
                  f"CUST-{oid[-4:]}", product, "physical", amount, "INR",
                  delivered - 3 * DAY, "fulfilled", addr, addr, True)
    shipment = Shipment(oid, f"EK{rng.randint(10**8, 10**9)}IN", rng.choice(_COURIERS),
                        delivered - 2 * DAY, delivered, "delivered", f"{city} {pin}",
                        "Signed by recipient")
    comms = [
        Communication(oid, order.customer_id, delivered + DAY, "chat",
                      "I've been charged twice for the same order.", "inbound", id=1),
    ]
    refund = Refund(oid, order.payment_id, True, "none", None, None,
                    "duplicate capture identified internally, not yet refunded")
    docs = [
        EvidenceDocument(oid, "explanation_letter", f"{oid.lower()}-billing-note.txt",
                         "Internal billing note: a retry created a second successful capture "
                         "for this single order. The duplicate has not been refunded.", id=1),
    ]
    return (shipment, comms, refund, docs, order, "INDEFENSIBLE", "duplicate_transaction",
            "The merchant's own records confirm a genuine double charge that was never "
            "refunded. Delivery proof is irrelevant to the duplicate.")


_ARCHETYPES = [
    (_arch_upi_captured_but_claimed_failed, "upi_captured_claimed_failed"),
    (_arch_delivered_and_admitted, "delivered_and_admitted"),
    (_arch_digital_access_proven, "digital_access_proven"),
    (_arch_refund_already_processed, "refund_already_processed"),
    (_arch_partial_refund_disputed_full, "partial_refund_disputed_full"),
    (_arch_delivered_late_after_complaint, "delivered_late_after_complaint"),
    (_arch_never_shipped_ignored, "never_shipped_ignored"),
    (_arch_rto_unrefunded, "rto_unrefunded"),
    (_arch_charged_after_cancellation, "charged_after_cancellation"),
    (_arch_hub_scan_only, "hub_scan_only"),
    (_arch_admitted_defect_unresolved, "admitted_defect_unresolved"),
    (_arch_duplicate_charge_confirmed, "duplicate_charge_confirmed"),
]

# Realistic mix, deliberately not 50/50 (spec section 17).
#
# Weighted toward defensible because first-party/"friendly" misuse is the
# largest single driver of e-commerce disputes industry-wide - a merchant
# facing a dispute queue genuinely does have grounds on a majority of them.
# Held back from being lopsided because Indian fulfilment reality (RTO,
# unrefunded returns, hub-scan-only deliveries) produces a large minority of
# disputes the merchant simply cannot defend. Weights target 58% defensible;
# the default 200-case draw lands at 60% (sampling variance, not a bug).
# This is a modelling assumption, not a measured population - stated here so
# the number can be argued with rather than mistaken for data.
# Weights sum to 100, so each number reads directly as a percentage of the
# dataset. Defensible archetypes total 58, indefensible 42.
_ARCHETYPE_WEIGHTS = [
    13,  # upi_captured_claimed_failed   ) defensible
    14,  # delivered_and_admitted        )
    9,   # digital_access_proven         )
    8,   # refund_already_processed      )
    8,   # partial_refund_disputed_full  )
    6,   # delivered_late_after_complaint) = 58
    9,   # never_shipped_ignored         ) indefensible
    9,   # rto_unrefunded                )
    6,   # charged_after_cancellation    )
    7,   # hub_scan_only                 )
    6,   # admitted_defect_unresolved    )
    5,   # duplicate_charge_confirmed    ) = 42
]


def generate_dataset(
    total: int = 200,
    holdout: int = 50,
    dataset_version: str = DATASET_VERSION,
) -> list[EvaluationCase]:
    """Build the full labelled dataset, deterministically.

    The same (total, holdout, dataset_version) always returns byte-identical
    cases - a re-run has to measure the same thing, or the numbers aren't
    comparable. The dev/holdout split is assigned by position after a seeded
    shuffle, so the holdout set isn't a biased tail of one archetype.
    """
    seed = int(hashlib.sha256(f"{dataset_version}:{total}:{holdout}".encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    now = _EPOCH
    policies = _policies(now)

    cases: list[EvaluationCase] = []
    for i in range(total):
        idx = rng.choices(range(len(_ARCHETYPES)), weights=_ARCHETYPE_WEIGHTS, k=1)[0]
        builder, archetype = _ARCHETYPES[idx]
        case_id = f"EVAL-{dataset_version}-{i:04d}"
        # Per-case RNG so one case's draws can't shift another's - editing a
        # single archetype must not reshuffle the entire dataset.
        case_rng = random.Random(
            int(hashlib.sha256(f"{seed}:{case_id}".encode()).hexdigest()[:8], 16)
        )
        shipment, comms, refund, docs, order, truth, reason_code, rationale = builder(
            case_id, now, case_rng
        )
        record = _build_case_record(case_id, order.amount, reason_code, now, case_rng)
        evidence = CaseEvidence(
            order=order, shipment=shipment, communications=comms,
            refund=refund, documents=docs, policies=policies,
        )
        cases.append(EvaluationCase(
            case_id=case_id, archetype=archetype, ground_truth=truth,
            split="dev", case=record, evidence=evidence, label_rationale=rationale,
        ))

    # Stratified split, by archetype.
    #
    # A plain shuffle-and-slice drifts badly at this size: the first version
    # of this produced a holdout that was 68% defensible against a 60%
    # population, which would quietly bias every headline number computed
    # from it. Taking a proportional slice of each archetype keeps the
    # holdout looking like the dataset it's sampled from.
    by_archetype: dict[str, list[int]] = {}
    for i, c in enumerate(cases):
        by_archetype.setdefault(c.archetype, []).append(i)

    holdout_ids: set[int] = set()
    target_fraction = holdout / total if total else 0.0
    # Largest archetypes first, so rounding leftovers land where they distort
    # the proportions least.
    for archetype in sorted(by_archetype, key=lambda a: -len(by_archetype[a])):
        idxs = by_archetype[archetype][:]
        rng.shuffle(idxs)
        take = round(len(idxs) * target_fraction)
        holdout_ids.update(idxs[:take])

    # Rounding can leave the holdout a case or two off; correct it against the
    # remaining pool rather than leaving the requested size unmet.
    remaining = [i for i in range(total) if i not in holdout_ids]
    rng.shuffle(remaining)
    while len(holdout_ids) < holdout and remaining:
        holdout_ids.add(remaining.pop())
    while len(holdout_ids) > holdout:
        holdout_ids.pop()

    return [
        EvaluationCase(
            case_id=c.case_id, archetype=c.archetype, ground_truth=c.ground_truth,
            split="holdout" if i in holdout_ids else "dev",
            case=c.case, evidence=c.evidence, label_rationale=c.label_rationale,
        )
        for i, c in enumerate(cases)
    ]


def dataset_fingerprint(cases: list[EvaluationCase]) -> str:
    """Content hash of the dataset.

    Folded into the run id so that changing how cases are generated can never
    silently mix old results with new ones in the same confusion matrix - a
    different dataset is a different experiment, and gets a different run.
    """
    material = "|".join(
        f"{c.case_id}:{c.archetype}:{c.ground_truth}:{c.split}:{c.case.amount}"
        for c in sorted(cases, key=lambda c: c.case_id)
    )
    return hashlib.sha256(material.encode()).hexdigest()[:10]


def dataset_summary(cases: list[EvaluationCase]) -> dict:
    """Composition of the dataset - for the report, and for sanity-checking
    that the generator didn't drift into something lopsided."""
    by_truth: dict[str, int] = {}
    by_arch: dict[str, int] = {}
    by_split: dict[str, int] = {}
    for c in cases:
        by_truth[c.ground_truth] = by_truth.get(c.ground_truth, 0) + 1
        by_arch[c.archetype] = by_arch.get(c.archetype, 0) + 1
        by_split[c.split] = by_split.get(c.split, 0) + 1
    total = len(cases)
    return {
        "total": total,
        "by_ground_truth": by_truth,
        "defensible_pct": round(100 * by_truth.get("DEFENSIBLE", 0) / total, 1) if total else 0,
        "by_split": by_split,
        "by_archetype": dict(sorted(by_arch.items())),
        "total_disputed_amount": sum(c.case.amount for c in cases),
    }
