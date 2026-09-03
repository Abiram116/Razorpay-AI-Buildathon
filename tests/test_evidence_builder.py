"""Phase 5: category selection, summary length enforcement, PDF generation."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pypdf import PdfReader

from src.config import load_settings
from src.database import CaseRecord
from src.document_generator import (
    _to_renderable,
    generate_case_report_pdf,
    generate_evidence_document_pdf,
    generate_explanation_letter_pdf,
)
from src.evidence_builder import (
    RAZORPAY_EVIDENCE_CATEGORIES,
    EvidenceBuildError,
    build_contest_summary,
    build_evidence_package,
    build_explanation_letter,
    select_evidence_categories,
)
from src.investigation_schema import EvidenceCitation, InvestigationResult
from src.merchant_db import (
    CaseEvidence,
    Communication,
    EvidenceDocument,
    Order,
    Policy,
    Refund,
    Shipment,
)


@pytest.fixture()
def settings(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abcdefghijklmn")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "x")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake")
    return load_settings(require_razorpay=False)


def _case(**overrides) -> CaseRecord:
    defaults = dict(
        dispute_id="sim_disp_test00000001", payment_id="sim_pay_test000000001",
        order_id="sim_order_test00001", amount=1_500_000, currency="INR",
        reason_code="goods_services_not_provided", respond_by=9_999_999_999,
        dispute_status="open", phase="chargeback", case_state="ANALYSIS_COMPLETE",
        source="simulated", is_simulated=True, ingested_at=1000,
    )
    defaults.update(overrides)
    return CaseRecord(**defaults)


def _order(**overrides) -> Order:
    defaults = dict(
        merchant_order_id="ORD-T1", razorpay_order_id="sim_order_test00001",
        payment_id="sim_pay_test000000001", customer_id="CUST-1", product="Smartphone",
        product_type="physical", amount=1_500_000, currency="INR", order_timestamp=1000,
        order_status="fulfilled", shipping_address="Addr", billing_address="Addr",
        is_simulated=True,
    )
    defaults.update(overrides)
    return Order(**defaults)


def _evidence(**overrides) -> CaseEvidence:
    defaults = dict(
        order=_order(),
        shipment=Shipment("ORD-T1", "TRK1", "BlueDart", 1100, 1200, "delivered",
                          "Bengaluru", "Signed by recipient"),
        communications=[Communication("ORD-T1", "CUST-1", 1300, "chat",
                                       "I received it", "inbound", id=1)],
        refund=Refund("ORD-T1", "sim_pay_test000000001", False, "none", None, None, None),
        documents=[EvidenceDocument("ORD-T1", "shipping_proof", "pod.txt",
                                     "Signed proof of delivery", id=1)],
        policies=[Policy("refund_policy", "v1", 500, "7 day returns"),
                  Policy("terms_and_conditions", "v1", 500, "Risk passes on delivery")],
    )
    defaults.update(overrides)
    return CaseEvidence(**defaults)


def _investigation(**overrides) -> InvestigationResult:
    defaults = dict(
        dispute_id="sim_disp_test00000001", classification="STRONG_CASE", confidence=0.93,
        executive_summary="Delivery is proven and the customer confirmed receipt.",
        reason="Courier records show signed delivery.",
        supporting_evidence=[EvidenceCitation("shipment", "ORD-T1", "signed delivery")],
        missing_evidence=[], conflicting_evidence=[], recommended_action="CONTEST",
        risk_factors=[], investigation_timestamp=2000, model="test-model",
        is_simulated_case=True,
    )
    defaults.update(overrides)
    return InvestigationResult(**defaults)


# ----------------------------------------------------------------------
# category selection - only real, relevant evidence
# ----------------------------------------------------------------------

def test_only_documented_razorpay_categories_are_emitted():
    categories, _ = select_evidence_categories(_case(), _evidence())
    for category in categories:
        assert category in RAZORPAY_EVIDENCE_CATEGORIES


def test_physical_delivered_order_gets_shipping_proof():
    categories, warnings = select_evidence_categories(_case(), _evidence())
    assert "shipping_proof" in categories
    assert "shipment:ORD-T1" in categories["shipping_proof"]
    assert warnings == []


def test_never_shipped_produces_no_shipping_proof_but_a_warning():
    """Including a 'never_shipped' record as shipping proof would actively
    damage the merchant's case - it must be excluded and flagged."""
    evidence = _evidence(
        shipment=Shipment("ORD-T1", None, None, None, None, "never_shipped", None, None),
        documents=[],
    )
    categories, warnings = select_evidence_categories(_case(), evidence)
    assert "shipping_proof" not in categories
    assert any("never shipped" in w for w in warnings)


def test_digital_product_uses_access_logs_not_shipping():
    evidence = _evidence(
        order=_order(product_type="digital", product="Online course"),
        shipment=None,
        documents=[
            EvidenceDocument("ORD-T1", "access_activity_log", "log.txt", "14 logins", id=2),
            EvidenceDocument("ORD-T1", "proof_of_service", "svc.txt", "60% complete", id=3),
        ],
    )
    categories, warnings = select_evidence_categories(_case(), evidence)
    assert "access_activity_log" in categories
    assert "proof_of_service" in categories
    assert "shipping_proof" not in categories
    assert warnings == []  # a digital order with no shipment is expected, not a gap


def test_digital_product_without_service_proof_is_flagged():
    evidence = _evidence(
        order=_order(product_type="digital"), shipment=None, documents=[],
    )
    _, warnings = select_evidence_categories(_case(), evidence)
    assert any("proof of service" in w.lower() for w in warnings)


def test_physical_product_with_no_shipment_record_at_all_is_flagged():
    evidence = _evidence(shipment=None, documents=[])
    _, warnings = select_evidence_categories(_case(), evidence)
    assert any("no shipment record" in w.lower() for w in warnings)


def test_unprocessed_refund_is_not_offered_as_refund_confirmation():
    evidence = _evidence(
        refund=Refund("ORD-T1", "sim_pay_test000000001", True, "pending", None, None, "x"),
    )
    categories, _ = select_evidence_categories(_case(), evidence)
    assert "refund_confirmation" not in categories


def test_processed_refund_is_offered_as_refund_confirmation():
    evidence = _evidence(
        refund=Refund("ORD-T1", "sim_pay_test000000001", True, "processed", 1000, 1500, "x"),
    )
    categories, _ = select_evidence_categories(_case(), evidence)
    assert "refund_confirmation" in categories


def test_policies_map_to_their_razorpay_categories():
    categories, _ = select_evidence_categories(_case(), _evidence())
    assert "refund_cancellation_policy" in categories
    # Razorpay's own spelling is singular "term_and_conditions"
    assert "term_and_conditions" in categories


def test_empty_categories_are_never_padded():
    """A category with nothing behind it must be absent, not present-and-empty."""
    evidence = _evidence(shipment=None, documents=[], communications=[], policies=[])
    categories, _ = select_evidence_categories(_case(), evidence)
    assert all(refs for refs in categories.values())
    assert "cancellation_proof" not in categories


# ----------------------------------------------------------------------
# contest summary length pipeline (spec section 20)
# ----------------------------------------------------------------------

def test_short_summary_passes_through_untouched(settings):
    summary, trace = build_contest_summary(_case(), _evidence(), _investigation(), settings)
    assert trace.within_limit
    assert not trace.was_shortened_by_ai
    assert not trace.was_truncated
    assert len(summary) == trace.final_length


def test_oversized_summary_is_shortened_by_ai(settings):
    long_summary = "x" * 3000
    investigation = _investigation(executive_summary=long_summary)

    with patch("src.evidence_builder._shorten_with_ai", return_value="A concise summary."):
        summary, trace = build_contest_summary(_case(), _evidence(), investigation, settings)

    assert summary == "A concise summary."
    assert trace.was_shortened_by_ai
    assert not trace.was_truncated
    assert trace.within_limit


def test_truncates_when_ai_shortening_is_unavailable(settings):
    investigation = _investigation(executive_summary="y" * 3000)

    with patch("src.evidence_builder._shorten_with_ai", return_value=None):
        summary, trace = build_contest_summary(_case(), _evidence(), investigation, settings)

    assert trace.was_truncated
    assert trace.within_limit
    assert len(summary) <= settings.contest_summary_max_chars
    assert "[truncated]" in summary


def test_truncates_when_ai_shortening_is_still_too_long(settings):
    """The model can ignore the limit; we must re-validate, not trust it."""
    investigation = _investigation(executive_summary="z" * 3000)

    with patch("src.evidence_builder._shorten_with_ai", return_value="w" * 2000):
        summary, trace = build_contest_summary(_case(), _evidence(), investigation, settings)

    assert trace.was_truncated
    assert len(summary) <= settings.contest_summary_max_chars


def test_summary_never_exceeds_the_documented_razorpay_limit(settings):
    for length in (500, 1500, 5000):
        investigation = _investigation(executive_summary="a" * length)
        with patch("src.evidence_builder._shorten_with_ai", return_value=None):
            summary, _ = build_contest_summary(_case(), _evidence(), investigation, settings)
        assert len(summary) <= settings.contest_summary_max_chars


# ----------------------------------------------------------------------
# explanation letter
# ----------------------------------------------------------------------

def test_explanation_letter_includes_citations_and_gaps():
    investigation = _investigation(
        missing_evidence=["recipient signature"],
        conflicting_evidence=["status says delivered but no signature"],
    )
    letter = build_explanation_letter(_case(), _evidence(), investigation)
    assert "sim_disp_test00000001" in letter
    assert "shipment:ORD-T1" in letter
    assert "recipient signature" in letter
    assert "status says delivered but no signature" in letter


# ----------------------------------------------------------------------
# package assembly
# ----------------------------------------------------------------------

def test_no_case_refuses_to_build_a_contest_package(settings, tmp_path):
    investigation = _investigation(
        classification="NO_CASE", recommended_action="DO_NOT_CONTEST",
        supporting_evidence=[],
    )
    with pytest.raises(EvidenceBuildError, match="NO_CASE"):
        build_evidence_package(
            _case(), _evidence(), investigation, settings, output_dir=tmp_path,
        )


def test_no_case_can_be_overridden_and_the_override_is_recorded(settings, tmp_path):
    investigation = _investigation(
        classification="NO_CASE", recommended_action="DO_NOT_CONTEST",
        supporting_evidence=[],
    )
    package = build_evidence_package(
        _case(), _evidence(), investigation, settings,
        output_dir=tmp_path, source_dir=tmp_path, force=True,
    )
    assert any("HUMAN OVERRIDE" in w for w in package.warnings)
    assert package.contest_advised is False


def test_package_is_not_submittable_without_documents(settings, tmp_path):
    """Razorpay requires >= 1 document id; a package with none must say so."""
    evidence = _evidence(documents=[])
    package = build_evidence_package(
        _case(), evidence, _investigation(), settings,
        output_dir=tmp_path, source_dir=tmp_path,
    )
    assert package.generated_documents == []
    assert not package.is_submittable
    assert any("at least one document" in w for w in package.warnings)


def test_package_generates_pdfs_and_is_submittable(settings, tmp_path):
    package = build_evidence_package(
        _case(), _evidence(), _investigation(), settings,
        output_dir=tmp_path, source_dir=tmp_path,
    )
    assert package.is_submittable
    assert len(package.generated_documents) == 1
    assert package.generated_documents[0].path.exists()
    assert package.generated_documents[0].path.suffix == ".pdf"


def test_package_serialises_for_storage(settings, tmp_path):
    package = build_evidence_package(
        _case(), _evidence(), _investigation(), settings,
        output_dir=tmp_path, source_dir=tmp_path,
    )
    data = package.to_dict()
    assert data["dispute_id"] == "sim_disp_test00000001"
    assert data["summary_trace"]["limit"] == 1000
    assert "shipping_proof" in data["evidence_categories"]


# ----------------------------------------------------------------------
# document generation
# ----------------------------------------------------------------------

def test_evidence_pdf_is_valid_and_contains_the_record(tmp_path):
    doc = EvidenceDocument("ORD-T1", "shipping_proof", "pod.txt", "Signed proof of delivery", id=1)
    generated = generate_evidence_document_pdf(doc, _evidence(), tmp_path, tmp_path)
    assert generated.path.exists()
    text = PdfReader(str(generated.path)).pages[0].extract_text()
    assert "shipping_proof" in text
    assert "ORD-T1" in text
    assert "Signed proof of delivery" in text


def test_missing_source_file_is_stated_not_faked(tmp_path):
    """If the underlying file is gone, say so - never imply it was read."""
    doc = EvidenceDocument("ORD-T1", "shipping_proof", "does-not-exist.txt", "desc", id=1)
    generated = generate_evidence_document_pdf(doc, _evidence(), tmp_path, tmp_path)
    text = PdfReader(str(generated.path)).pages[0].extract_text()
    assert "not found" in text.lower()


def test_simulated_case_pdf_carries_a_visible_warning(tmp_path):
    doc = EvidenceDocument("ORD-T1", "shipping_proof", "pod.txt", "desc", id=1)
    generated = generate_evidence_document_pdf(doc, _evidence(), tmp_path, tmp_path)
    text = PdfReader(str(generated.path)).pages[0].extract_text()
    assert "SIMULATED TEST DISPUTE" in text


def test_case_report_contains_ai_finding_and_human_review_notice(tmp_path):
    generated = generate_case_report_pdf(
        _case(), _evidence(), _investigation(), tmp_path,
        contest_summary="A summary", included_categories={"shipping_proof": ["shipment:ORD-T1"]},
    )
    text = "".join(p.extract_text() for p in PdfReader(str(generated.path)).pages)
    assert "STRONG_CASE" in text
    assert "human" in text.lower()
    assert "No dispute has been contested" in text


def test_explanation_letter_pdf_renders(tmp_path):
    letter_text = build_explanation_letter(_case(), _evidence(), _investigation())
    generated = generate_explanation_letter_pdf(
        _case(), _evidence(), _investigation(), letter_text, tmp_path,
    )
    assert generated.document_type == "explanation_letter"
    text = PdfReader(str(generated.path)).pages[0].extract_text()
    assert "Explanation Letter" in text


def test_html_special_characters_in_customer_messages_do_not_break_the_pdf(tmp_path):
    """Customer text is arbitrary; <b> or & must not corrupt the layout."""
    evidence = _evidence(communications=[
        Communication("ORD-T1", "CUST-1", 1300, "chat",
                      "I ordered <b>2</b> items & got 1! 3 > 1", "inbound", id=1),
    ])
    generated = generate_case_report_pdf(_case(), evidence, _investigation(), tmp_path)
    text = "".join(p.extract_text() for p in PdfReader(str(generated.path)).pages)
    assert "got 1" in text


def test_unicode_from_the_model_is_transliterated_not_boxed():
    """reportlab's built-in fonts are Latin-1; real Groq output contains
    U+2011/U+2013/U+202F, which would render as black boxes."""
    assert _to_renderable("OTP‑verified") == "OTP-verified"
    assert _to_renderable("a–b") == "a-b"
    assert _to_renderable("x y") == "x y"
    assert all(ord(c) < 256 for c in _to_renderable("emoji \U0001F600 and ₹500"))


def test_model_unicode_survives_into_a_readable_pdf(tmp_path):
    investigation = _investigation(
        executive_summary="Delivered with OTP‑verified signature – confirmed.",
    )
    generated = generate_case_report_pdf(_case(), _evidence(), investigation, tmp_path)
    text = "".join(p.extract_text() for p in PdfReader(str(generated.path)).pages)
    assert "OTP-verified" in text
