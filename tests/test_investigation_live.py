"""Optional live Groq integration test.

Skipped by default so the normal suite stays fast, offline, and free. Run it
deliberately:

    RUN_GROQ_INTEGRATION=1 uv run pytest tests/test_investigation_live.py -v

This asserts the properties that must hold against the REAL model rather
than a mock: that a strict json_schema response comes back at all, that it
validates, and - most importantly - that every citation the live model
produces resolves to a real record. It deliberately does NOT assert a
specific classification: pinning the live model to one label would make this
a flaky test of the model's judgment rather than a test of our contract.
"""

from __future__ import annotations

import os

import pytest

from src.config import load_settings
from src.database import init_case_db, ingest_case
from src.dispute_schema import DisputeEntity, IngestedCase, PaymentSummary
from src.investigation_agent import investigate
from src.investigation_schema import CLASSIFICATIONS, available_evidence_refs
from src.merchant_db import (
    Communication,
    Order,
    Refund,
    Shipment,
    get_case_evidence,
    init_merchant_db,
    insert_communication,
    insert_order,
    insert_refund,
    insert_shipment,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_GROQ_INTEGRATION") != "1",
    reason="live Groq test - set RUN_GROQ_INTEGRATION=1 to enable",
)

PAY_ID = "sim_pay_liveintegrat1"
DISP_ID = "sim_disp_liveintegra1"


@pytest.fixture()
def live_case(tmp_path):
    case_db, merchant_db = tmp_path / "cases.db", tmp_path / "merchant.db"
    init_case_db(case_db)
    init_merchant_db(merchant_db)

    dispute = DisputeEntity(
        id=DISP_ID, payment_id=PAY_ID, amount=1_500_000, currency="INR", amount_deducted=0,
        reason_code="goods_services_not_provided", respond_by=9_999_999_999,
        status="open", phase="chargeback", created_at=1000,
    )
    payment = PaymentSummary(
        id=PAY_ID, order_id="sim_order_liveinteg1", amount=1_500_000, currency="INR",
        status="captured", method="card", captured=True, amount_refunded=0,
        refund_status=None, created_at=900,
    )
    case = ingest_case(
        case_db,
        IngestedCase(dispute=dispute, payment=payment, source="simulated", is_simulated=True),
        actor="live-test",
    )

    insert_order(merchant_db, Order(
        "ORD-LIVE", "sim_order_liveinteg1", PAY_ID, "CUST-L", "Smartphone", "physical",
        1_500_000, "INR", 1000, "fulfilled", "Addr", "Addr", True,
    ))
    insert_shipment(merchant_db, Shipment(
        "ORD-LIVE", "TRK-LIVE", "BlueDart", 1100, 1200, "delivered", "Bengaluru",
        "Signed by recipient",
    ))
    insert_communication(merchant_db, Communication(
        "ORD-LIVE", "CUST-L", 1300, "chat", "I received the package, thank you!", "inbound",
    ))
    insert_refund(merchant_db, Refund("ORD-LIVE", PAY_ID, False, "none", None, None, None))

    return case, get_case_evidence(merchant_db, payment_id=PAY_ID)


def test_live_model_returns_a_valid_structured_investigation(live_case):
    case, evidence = live_case
    settings = load_settings(require_razorpay=False)
    if not settings.ai.api_key:
        pytest.skip("GROQ_API_KEY not configured")

    result = investigate(case, evidence, settings)

    assert result.succeeded, getattr(result, "detail", "investigation failed")
    assert result.classification in CLASSIFICATIONS
    assert 0.0 <= result.confidence <= 1.0
    assert result.executive_summary.strip()
    assert result.dispute_id == DISP_ID


def test_live_model_citations_all_resolve_to_real_records(live_case):
    """The anti-hallucination guarantee, against the real model."""
    case, evidence = live_case
    settings = load_settings(require_razorpay=False)
    if not settings.ai.api_key:
        pytest.skip("GROQ_API_KEY not configured")

    allowed = available_evidence_refs(
        evidence, dispute_id=case.dispute_id, payment_id=case.payment_id
    )
    result = investigate(case, evidence, settings)

    assert result.succeeded, getattr(result, "detail", "investigation failed")
    for citation in result.supporting_evidence:
        assert citation.reference in allowed, f"model cited unknown record {citation.reference}"
