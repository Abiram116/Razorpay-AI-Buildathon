"""Phase 4: the investigator's contract, failure modes, and citation integrity.

No test here makes a live Groq call. The model is mocked so behaviour is
deterministic; a separately-gated live test lives in
tests/test_investigation_live.py.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import src.investigation_agent as agent
from src.config import load_settings
from src.database import (
    get_latest_investigation,
    ingest_case,
    init_case_db,
    save_investigation,
)
from src.dispute_schema import DisputeEntity, IngestedCase, PaymentSummary
from src.investigation_schema import (
    InvestigationFailure,
    InvestigationValidationError,
    available_evidence_refs,
    validate_investigation_response,
)
from src.merchant_db import (
    Communication,
    EvidenceDocument,
    Order,
    Policy,
    Refund,
    Shipment,
    get_case_evidence,
    init_merchant_db,
    insert_communication,
    insert_document,
    insert_order,
    insert_policy,
    insert_refund,
    insert_shipment,
)

PAY_ID = "sim_pay_testpayment01"
DISP_ID = "sim_disp_testdispute1"


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------

@pytest.fixture()
def settings(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abcdefghijklmn")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "x")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake_key_for_tests")
    return load_settings(require_razorpay=False)


@pytest.fixture()
def dbs(tmp_path):
    case_db = tmp_path / "cases.db"
    merchant_db = tmp_path / "merchant.db"
    init_case_db(case_db)
    init_merchant_db(merchant_db)
    return case_db, merchant_db


def _seed_case(case_db, amount=1_500_000, reason="goods_services_not_provided"):
    dispute = DisputeEntity(
        id=DISP_ID, payment_id=PAY_ID, amount=amount, currency="INR", amount_deducted=0,
        reason_code=reason, respond_by=9_999_999_999, status="open", phase="chargeback",
        created_at=1000,
    )
    payment = PaymentSummary(
        id=PAY_ID, order_id="sim_order_testorder1", amount=amount, currency="INR",
        status="captured", method="card", captured=True, amount_refunded=0,
        refund_status=None, created_at=900,
    )
    return ingest_case(
        case_db,
        IngestedCase(dispute=dispute, payment=payment, source="simulated", is_simulated=True),
        actor="test",
    )


def _seed_strong_evidence(merchant_db):
    insert_order(merchant_db, Order(
        "ORD-S1", "sim_order_testorder1", PAY_ID, "CUST-1", "Smartphone", "physical",
        1_500_000, "INR", 1000, "fulfilled", "Addr", "Addr", True,
    ))
    insert_shipment(merchant_db, Shipment(
        "ORD-S1", "TRK1", "BlueDart", 1100, 1200, "delivered", "Bengaluru", "Signed by recipient",
    ))
    insert_communication(merchant_db, Communication(
        "ORD-S1", "CUST-1", 1300, "chat", "I received the package, thanks!", "inbound",
    ))
    insert_refund(merchant_db, Refund("ORD-S1", PAY_ID, False, "none", None, None, None))
    insert_document(merchant_db, EvidenceDocument(
        "ORD-S1", "shipping_proof", "pod.txt", "Signed proof of delivery",
    ))
    insert_policy(merchant_db, Policy("refund_policy", "v1", 500, "7 day returns"))


def _seed_no_case_evidence(merchant_db):
    insert_order(merchant_db, Order(
        "ORD-N1", "sim_order_testorder1", PAY_ID, "CUST-2", "Speaker", "physical",
        1_500_000, "INR", 1000, "confirmed", "Addr", "Addr", True,
    ))
    insert_shipment(merchant_db, Shipment(
        "ORD-N1", None, None, None, None, "never_shipped", None, None,
    ))
    insert_communication(merchant_db, Communication(
        "ORD-N1", "CUST-2", 1300, "email", "Where is my order?", "inbound",
    ))


def _model_response(**overrides):
    payload = {
        "classification": "STRONG_CASE",
        "confidence": 0.93,
        "executive_summary": "Delivery is proven and the customer confirmed receipt.",
        "reason": "Courier records show signed delivery; customer acknowledged receipt.",
        "supporting_evidence": [
            {"reference": "shipment:ORD-S1", "note": "signed delivery"},
            {"reference": "order:ORD-S1", "note": "order fulfilled"},
        ],
        "missing_evidence": [],
        "conflicting_evidence": [],
        "recommended_action": "CONTEST",
        "risk_factors": [],
    }
    payload.update(overrides)
    return payload


# ----------------------------------------------------------------------
# the three classifications
# ----------------------------------------------------------------------

def test_strong_case_scenario(settings, dbs):
    case_db, merchant_db = dbs
    case = _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    with patch.object(agent, "_call_groq", return_value=_model_response()):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)

    assert result.succeeded
    assert result.classification == "STRONG_CASE"
    assert result.recommended_action == "CONTEST"
    assert result.dispute_id == DISP_ID
    assert result.is_simulated_case is True


def test_no_case_scenario(settings, dbs):
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_no_case_evidence(merchant_db)

    response = _model_response(
        classification="NO_CASE", recommended_action="DO_NOT_CONTEST", confidence=0.95,
        executive_summary="Never shipped; merchant cannot show fulfilment.",
        reason="Shipment record shows never_shipped and no delivery evidence exists.",
        supporting_evidence=[{"reference": "shipment:ORD-N1", "note": "never shipped"}],
        missing_evidence=["proof of shipment", "tracking number"],
    )
    with patch.object(agent, "_call_groq", return_value=response):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)

    assert result.succeeded
    assert result.classification == "NO_CASE"
    assert result.recommended_action == "DO_NOT_CONTEST"
    assert "proof of shipment" in result.missing_evidence


def test_weak_case_scenario(settings, dbs):
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    response = _model_response(
        classification="WEAK_CASE", recommended_action="MANUAL_REVIEW", confidence=0.55,
        missing_evidence=["recipient signature"],
    )
    with patch.object(agent, "_call_groq", return_value=response):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)

    assert result.classification == "WEAK_CASE"
    assert result.recommended_action == "MANUAL_REVIEW"


# ----------------------------------------------------------------------
# evidence traceability - the anti-hallucination guarantee
# ----------------------------------------------------------------------

def test_hallucinated_evidence_reference_is_rejected(settings, dbs):
    """The core safety property: a citation to a record that does not exist
    must be refused, not passed through to a human as if it were real."""
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    hallucinated = _model_response(supporting_evidence=[
        {"reference": "document:9999", "note": "a signed affidavit that does not exist"},
    ])
    with patch.object(agent, "_call_groq", return_value=hallucinated):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)

    assert not result.succeeded
    assert result.failure_reason == "INVALID_AI_RESPONSE"


def test_every_returned_citation_resolves_to_a_real_record(settings, dbs):
    case_db, merchant_db = dbs
    case = _seed_case(case_db)
    _seed_strong_evidence(merchant_db)
    evidence = get_case_evidence(merchant_db, payment_id=PAY_ID)
    allowed = available_evidence_refs(evidence, dispute_id=DISP_ID, payment_id=PAY_ID)

    with patch.object(agent, "_call_groq", return_value=_model_response()):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)

    assert result.succeeded
    for citation in result.supporting_evidence:
        assert citation.reference in allowed


def test_malformed_reference_format_is_rejected(settings, dbs):
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    bad = _model_response(supporting_evidence=[
        {"reference": "the order was delivered", "note": "prose, not a reference"},
    ])
    with patch.object(agent, "_call_groq", return_value=bad):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)
    assert not result.succeeded


def test_strong_case_with_no_citations_is_rejected(settings, dbs):
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    with patch.object(agent, "_call_groq", return_value=_model_response(supporting_evidence=[])):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)
    assert not result.succeeded


# ----------------------------------------------------------------------
# conflicting / missing evidence are surfaced, not smoothed over
# ----------------------------------------------------------------------

def test_conflicting_evidence_is_preserved_in_the_result(settings, dbs):
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    response = _model_response(
        classification="WEAK_CASE", recommended_action="MANUAL_REVIEW",
        conflicting_evidence=["delivery_status says delivered but no signature on file"],
    )
    with patch.object(agent, "_call_groq", return_value=response):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)

    assert result.conflicting_evidence == [
        "delivery_status says delivered but no signature on file"
    ]


def test_missing_merchant_evidence_fails_safe_without_investigating(settings, dbs):
    """No merchant record at all must NOT reach the model - there is nothing
    to reason about, and a 'finding' here would be pure invention."""
    case_db, merchant_db = dbs
    _seed_case(case_db)  # case exists, merchant DB is empty

    with patch.object(agent, "_call_groq") as mock_call:
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)

    mock_call.assert_not_called()
    assert not result.succeeded
    assert result.failure_reason == "NO_MERCHANT_EVIDENCE"
    assert "human review" in result.detail.lower()


def test_unknown_dispute_id_fails_safe(settings, dbs):
    case_db, merchant_db = dbs
    result = agent.investigate_dispute("sim_disp_doesnotexist", case_db, merchant_db, settings)
    assert not result.succeeded
    assert result.failure_reason == "CASE_NOT_FOUND"


# ----------------------------------------------------------------------
# model / API failure modes
# ----------------------------------------------------------------------

def test_malformed_json_from_model_fails_safe(settings, dbs):
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    with patch.object(agent, "_call_groq", side_effect=InvestigationValidationError("not JSON")):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)

    assert not result.succeeded
    assert result.failure_reason == "INVALID_AI_RESPONSE"


def test_invalid_classification_value_is_rejected(settings, dbs):
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    with patch.object(agent, "_call_groq", return_value=_model_response(classification="PROBABLY_FINE")):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)
    assert not result.succeeded


def test_out_of_range_confidence_is_rejected(settings, dbs):
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    with patch.object(agent, "_call_groq", return_value=_model_response(confidence=4.2)):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)
    assert not result.succeeded


def test_retry_once_then_succeed(settings, dbs):
    """A first bad response is fed back to the model; a corrected second
    response is accepted (spec section 10)."""
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    responses = [_model_response(classification="NONSENSE"), _model_response()]
    with patch.object(agent, "_call_groq", side_effect=responses) as mock_call:
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)

    assert result.succeeded
    assert mock_call.call_count == 2


def test_api_failure_fails_safe_as_unavailable(settings, dbs):
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    with patch.object(agent, "_call_groq", side_effect=agent.GroqUnavailable("boom")):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)

    assert not result.succeeded
    assert result.failure_reason == "AI_UNAVAILABLE"
    assert "manual review" in result.detail.lower()


def test_missing_api_key_fails_safe(dbs, monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abcdefghijklmn")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "x")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    settings = load_settings(require_razorpay=False)

    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)
    assert not result.succeeded
    assert result.failure_reason == "AI_UNAVAILABLE"
    assert "GROQ_API_KEY" in result.detail


def test_timeout_is_treated_as_transient_and_retried(settings, dbs):
    """A timeout must be waited out and retried, not immediately abandoned."""
    from groq import APITimeoutError

    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise agent.GroqTransientError("timed out", retry_after=0.01)
        return _model_response()

    with patch.object(agent, "_call_groq", side_effect=flaky), \
         patch.object(agent.time, "sleep"):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)

    assert result.succeeded
    assert calls["n"] == 2


def test_rate_limit_exhaustion_eventually_fails_safe(settings, dbs):
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    with patch.object(agent, "_call_groq",
                      side_effect=agent.GroqTransientError("rate limited", retry_after=0.01)), \
         patch.object(agent.time, "sleep"):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)

    assert not result.succeeded
    assert result.failure_reason == "AI_UNAVAILABLE"


def test_rate_limit_error_maps_to_transient_with_parsed_delay(settings):
    """The API tells us how long to wait; we must actually parse and use it."""
    from groq import RateLimitError

    fake_response = MagicMock(status_code=429, headers={})
    err = RateLimitError(
        "Error code: 429 - rate limit reached. Please try again in 7.545s.",
        response=fake_response, body=None,
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = err

    with patch("groq.Groq", return_value=fake_client):
        with pytest.raises(agent.GroqTransientError) as exc_info:
            agent._call_groq(settings, "sys", "user", "model")

    assert exc_info.value.retry_after == pytest.approx(7.545)


# ----------------------------------------------------------------------
# prompt construction & persistence
# ----------------------------------------------------------------------

def test_prompt_never_contains_the_expected_strength_label(settings, dbs):
    """The dev-only ground-truth label must never leak into the prompt."""
    case_db, merchant_db = dbs
    case = _seed_case(case_db)
    _seed_strong_evidence(merchant_db)
    evidence = get_case_evidence(merchant_db, payment_id=PAY_ID)

    prompt = agent.build_investigation_prompt(case, evidence)
    for label in ("STRONG_CASE", "WEAK_CASE", "NO_CASE", "expected_strength"):
        assert label not in prompt


def test_prompt_marks_absent_shipment_as_expected_for_digital(settings, dbs):
    case_db, merchant_db = dbs
    case = _seed_case(case_db)
    insert_order(merchant_db, Order(
        "ORD-D1", "sim_order_testorder1", PAY_ID, "CUST-3", "Course", "digital",
        1_500_000, "INR", 1000, "fulfilled", None, None, True,
    ))
    evidence = get_case_evidence(merchant_db, payment_id=PAY_ID)
    prompt = agent.build_investigation_prompt(case, evidence)
    assert "No shipment record exists" in prompt
    assert "digital" in prompt


def test_investigation_result_persists_and_reloads(settings, dbs):
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    with patch.object(agent, "_call_groq", return_value=_model_response()):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)
    save_investigation(case_db, result)

    stored = get_latest_investigation(case_db, DISP_ID)
    assert stored["succeeded"] is True
    assert stored["classification"] == "STRONG_CASE"
    assert stored["result"]["supporting_evidence"][0]["reference"] == "shipment:ORD-S1"


def test_failed_investigation_is_also_persisted(settings, dbs):
    """A failure must leave an auditable trace, not silently vanish."""
    case_db, merchant_db = dbs
    _seed_case(case_db)
    _seed_strong_evidence(merchant_db)

    with patch.object(agent, "_call_groq", side_effect=agent.GroqUnavailable("down")):
        result = agent.investigate_dispute(DISP_ID, case_db, merchant_db, settings)
    save_investigation(case_db, result)

    stored = get_latest_investigation(case_db, DISP_ID)
    assert stored["succeeded"] is False
    assert stored["failure_reason"] == "AI_UNAVAILABLE"
    assert stored["classification"] is None
