"""Phase 7: contest drafting, submission, and the safety boundary.

No live Razorpay call: the client is mocked throughout. The tests assert
both the happy path against a real (non-simulated) dispute and, more
importantly, that the blocks hold.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from src.config import load_settings
from src.contest_service import (
    ContestError,
    SubmissionBlocked,
    assert_submittable,
    build_contest_payload,
    build_local_draft,
    collect_uploadable_documents,
    save_draft_to_razorpay,
    submit_contest,
    upload_documents,
)
from src.database import (
    CaseRecord,
    get_audit_log,
    get_case,
    get_contest_attempts,
    get_uploaded_documents,
    ingest_case,
    init_case_db,
    transition_case_state,
)
from src.dispute_schema import DisputeEntity, IngestedCase, PaymentSummary
from src.evidence_builder import build_evidence_package
from src.investigation_schema import EvidenceCitation, InvestigationResult
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

REAL_DISP = "disp_AAAAAAAAAAAAAA"
REAL_PAY = "pay_BBBBBBBBBBBBBB"
SIM_DISP = "sim_disp_simulated01"
SIM_PAY = "sim_pay_simulated001"


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abcdefghijklmn")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "x")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake")
    s = load_settings(require_razorpay=False)
    from src.config import Paths
    object.__setattr__(s, "paths", Paths(
        merchant_db=tmp_path / "merchant.db", case_db=tmp_path / "cases.db",
        generated_docs=tmp_path / "generated",
    ))
    init_case_db(s.paths.case_db)
    init_merchant_db(s.paths.merchant_db)
    return s


def _seed_case(settings, *, simulated: bool) -> CaseRecord:
    disp, pay = (SIM_DISP, SIM_PAY) if simulated else (REAL_DISP, REAL_PAY)
    dispute = DisputeEntity(
        id=disp, payment_id=pay, amount=1_500_000, currency="INR", amount_deducted=0,
        reason_code="goods_services_not_provided", respond_by=int(time.time()) + 5 * 86400,
        status="open", phase="chargeback", created_at=1000,
    )
    payment = PaymentSummary(
        id=pay, order_id="order_CCCCCCCCCCCCCC" if not simulated else "sim_order_simulated1",
        amount=1_500_000, currency="INR", status="captured", method="card", captured=True,
        amount_refunded=0, refund_status=None, created_at=900,
    )
    return ingest_case(
        settings.paths.case_db,
        IngestedCase(
            dispute=dispute, payment=payment,
            source="simulated" if simulated else "razorpay_webhook",
            is_simulated=simulated,
        ),
        actor="test",
    )


def _seed_evidence(settings, *, simulated: bool):
    pay = SIM_PAY if simulated else REAL_PAY
    order_ref = "sim_order_simulated1" if simulated else "order_CCCCCCCCCCCCCC"
    db = settings.paths.merchant_db
    insert_order(db, Order(
        "ORD-C1", order_ref, pay, "CUST-1", "Smartphone", "physical",
        1_500_000, "INR", 1000, "fulfilled", "Addr", "Addr", simulated,
    ))
    insert_shipment(db, Shipment(
        "ORD-C1", "TRK1", "BlueDart", 1100, 1200, "delivered", "Bengaluru", "Signed",
    ))
    insert_communication(db, Communication(
        "ORD-C1", "CUST-1", 1300, "chat", "I received it", "inbound",
    ))
    insert_refund(db, Refund("ORD-C1", pay, False, "none", None, None, None))
    insert_document(db, EvidenceDocument(
        "ORD-C1", "shipping_proof", "pod.txt", "Signed proof of delivery",
    ))
    insert_policy(db, Policy("refund_policy", "v1", 500, "7 day returns"))
    return get_case_evidence(db, payment_id=pay)


def _investigation(dispute_id):
    return InvestigationResult(
        dispute_id=dispute_id, classification="STRONG_CASE", confidence=0.94,
        executive_summary="Delivery proven; customer confirmed receipt.",
        reason="Courier shows signed delivery.",
        supporting_evidence=[EvidenceCitation("shipment", "ORD-C1", "signed delivery")],
        missing_evidence=[], conflicting_evidence=[], recommended_action="CONTEST",
        risk_factors=[], investigation_timestamp=int(time.time()), model="test-model",
        is_simulated_case=False,
    )


@pytest.fixture()
def real_case(settings):
    case = _seed_case(settings, simulated=False)
    evidence = _seed_evidence(settings, simulated=False)
    investigation = _investigation(REAL_DISP)
    package = build_evidence_package(
        case, evidence, investigation, settings,
        output_dir=settings.paths.generated_docs / REAL_DISP,
        source_dir=settings.paths.generated_docs,
    )
    return case, evidence, package, investigation


@pytest.fixture()
def approved_real_case(settings, real_case):
    case, evidence, package, investigation = real_case
    db = settings.paths.case_db
    for state in ("ANALYZING", "ANALYSIS_COMPLETE", "PENDING_HUMAN_REVIEW", "APPROVED"):
        transition_case_state(db, case.dispute_id, state, actor="t", action="setup")
    return get_case(db, case.dispute_id), evidence, package, investigation


def _mock_client(doc_id_prefix="doc_"):
    client = MagicMock()
    counter = {"n": 0}

    def upload(path, mime):
        counter["n"] += 1
        return {"id": f"{doc_id_prefix}{counter['n']:014d}", "entity": "document"}

    client.upload_evidence_document.side_effect = upload
    client.contest_dispute.return_value = {
        "id": REAL_DISP, "entity": "dispute", "status": "under_review",
        "evidence": {"summary": "x", "submitted_at": int(time.time())},
    }
    return client


# ----------------------------------------------------------------------
# 8. simulated disputes are backend-blocked
# ----------------------------------------------------------------------

def test_simulated_dispute_is_blocked_by_assert_submittable(settings):
    case = _seed_case(settings, simulated=True)
    with pytest.raises(SubmissionBlocked, match="SIMULATED"):
        assert_submittable(case)


def test_real_webhook_dispute_passes_the_guard(settings):
    case = _seed_case(settings, simulated=False)
    assert_submittable(case)  # must not raise


def test_simulated_submission_never_touches_razorpay(settings):
    case = _seed_case(settings, simulated=True)
    evidence = _seed_evidence(settings, simulated=True)
    investigation = _investigation(SIM_DISP)
    package = build_evidence_package(
        case, evidence, investigation, settings,
        output_dir=settings.paths.generated_docs / SIM_DISP,
        source_dir=settings.paths.generated_docs,
    )
    client = _mock_client()

    with pytest.raises(SubmissionBlocked):
        submit_contest(case, evidence, package, investigation, actor="t",
                       human_confirmed=True, settings=settings, client=client)

    client.contest_dispute.assert_not_called()
    client.upload_evidence_document.assert_not_called()


def test_simulated_draft_is_marked_blocked_but_still_inspectable(settings):
    """A simulated case must still be demonstrable end to end locally."""
    case = _seed_case(settings, simulated=True)
    evidence = _seed_evidence(settings, simulated=True)
    investigation = _investigation(SIM_DISP)
    package = build_evidence_package(
        case, evidence, investigation, settings,
        output_dir=settings.paths.generated_docs / SIM_DISP,
        source_dir=settings.paths.generated_docs,
    )
    draft = build_local_draft(case, evidence, package, investigation, settings)
    assert draft.blocked_reason is not None
    assert not draft.can_submit
    assert draft.payload["summary"]
    assert draft.uploadable_documents


# ----------------------------------------------------------------------
# 7. submit requires explicit human action
# ----------------------------------------------------------------------

def test_submit_without_human_confirmation_is_refused(settings, approved_real_case):
    case, evidence, package, investigation = approved_real_case
    client = _mock_client()
    with pytest.raises(SubmissionBlocked, match="human_confirmed"):
        submit_contest(case, evidence, package, investigation, actor="alice",
                       settings=settings, client=client)
    client.contest_dispute.assert_not_called()


def test_submit_without_an_actor_is_refused(settings, approved_real_case):
    case, evidence, package, investigation = approved_real_case
    client = _mock_client()
    with pytest.raises(SubmissionBlocked, match="identity"):
        submit_contest(case, evidence, package, investigation, actor="  ",
                       human_confirmed=True, settings=settings, client=client)


def test_no_code_path_submits_from_a_strong_case_alone(settings, real_case):
    """A STRONG_CASE verdict must not be sufficient to reach submission."""
    case, evidence, package, investigation = real_case
    assert investigation.classification == "STRONG_CASE"
    client = _mock_client()
    draft = build_local_draft(case, evidence, package, investigation, settings)
    assert draft.payload["action"] == "draft"
    client.contest_dispute.assert_not_called()


# ----------------------------------------------------------------------
# 1-3. draft/submit API, uploads, categories
# ----------------------------------------------------------------------

def test_draft_calls_contest_api_with_action_draft(settings, approved_real_case):
    case, evidence, package, investigation = approved_real_case
    client = _mock_client()
    save_draft_to_razorpay(case, evidence, package, investigation,
                           actor="alice", settings=settings, client=client)

    client.contest_dispute.assert_called_once()
    dispute_id, payload = client.contest_dispute.call_args[0]
    assert dispute_id == REAL_DISP
    assert payload["action"] == "draft"


def test_submit_calls_contest_api_with_action_submit(settings, approved_real_case):
    case, evidence, package, investigation = approved_real_case
    db = settings.paths.case_db
    client = _mock_client()
    save_draft_to_razorpay(case, evidence, package, investigation,
                           actor="alice", settings=settings, client=client)
    drafted = get_case(db, REAL_DISP)

    submit_contest(drafted, evidence, package, investigation, actor="alice",
                   human_confirmed=True, settings=settings, client=client)

    _, payload = client.contest_dispute.call_args[0]
    assert payload["action"] == "submit"


def test_documents_are_uploaded_with_dispute_evidence_purpose(settings, approved_real_case):
    case, evidence, package, investigation = approved_real_case
    client = _mock_client()
    save_draft_to_razorpay(case, evidence, package, investigation,
                           actor="alice", settings=settings, client=client)
    assert client.upload_evidence_document.call_count >= 1
    for call in client.upload_evidence_document.call_args_list:
        path, mime = call[0]
        assert path.endswith(".pdf")
        assert mime == "application/pdf"


def test_payload_uses_documented_razorpay_categories_only(settings, approved_real_case):
    from src.evidence_builder import RAZORPAY_EVIDENCE_CATEGORIES
    case, evidence, package, investigation = approved_real_case
    client = _mock_client()
    save_draft_to_razorpay(case, evidence, package, investigation,
                           actor="alice", settings=settings, client=client)
    _, payload = client.contest_dispute.call_args[0]

    reserved = {"summary", "action", "amount"}
    for key in payload:
        assert key in reserved or key in RAZORPAY_EVIDENCE_CATEGORIES, key


def test_payload_carries_real_document_ids(settings, approved_real_case):
    case, evidence, package, investigation = approved_real_case
    client = _mock_client()
    save_draft_to_razorpay(case, evidence, package, investigation,
                           actor="alice", settings=settings, client=client)
    _, payload = client.contest_dispute.call_args[0]

    doc_ids = [
        v for k, values in payload.items()
        if isinstance(values, list) and k not in {"others"}
        for v in values
    ]
    assert doc_ids
    assert all(d.startswith("doc_") for d in doc_ids)


def test_explanation_letter_and_policies_reach_the_payload(settings, approved_real_case):
    """These have real content but no PDF until the contest service makes one;
    without that step they would silently drop out of the payload."""
    case, evidence, package, investigation = approved_real_case
    documents = collect_uploadable_documents(
        case, evidence, package, investigation, settings.paths.generated_docs / REAL_DISP
    )
    types = {d.document_type for d in documents}
    assert "explanation_letter" in types
    assert "refund_cancellation_policy" in types


def test_billing_proof_is_rendered_not_left_unsendable(settings, approved_real_case):
    """billing_proof is cited for every case (evidence_builder cites the order
    record unconditionally) but an order row is not a file. Regression test
    for a real gap: this category reached the dashboard's "cited but not
    sendable" warning on every single case until a document generator for it
    was added."""
    case, evidence, package, investigation = approved_real_case
    assert "billing_proof" in package.evidence_categories

    documents = collect_uploadable_documents(
        case, evidence, package, investigation, settings.paths.generated_docs / REAL_DISP
    )
    assert any(d.document_type == "billing_proof" for d in documents)


def test_local_draft_leaves_no_evidence_category_unsendable_for_a_full_case(
    settings, approved_real_case
):
    """End-to-end check on a case with every evidence type present: nothing
    cited in the package may be absent from the draft's document ids."""
    from src.contest_service import build_local_draft
    case, evidence, package, investigation = approved_real_case
    draft = build_local_draft(case, evidence, package, investigation, settings)
    assert draft.unsupported_categories == {}


def test_empty_categories_are_never_sent(settings):
    payload = build_contest_payload(
        "summary", {"shipping_proof": ["doc_1"], "billing_proof": []},
        "draft", load_settings(require_razorpay=False),
    )
    assert "shipping_proof" in payload
    assert "billing_proof" not in payload


def test_others_category_uses_the_documented_object_shape(settings):
    payload = build_contest_payload(
        "summary", {"others": ["doc_1", "doc_2"]}, "draft", settings,
    )
    assert payload["others"] == [{"type": "supporting_document", "document_ids": ["doc_1", "doc_2"]}]


# ----------------------------------------------------------------------
# 4. summary limit
# ----------------------------------------------------------------------

def test_oversized_summary_is_refused_before_sending(settings):
    with pytest.raises(ContestError, match="1000-character limit"):
        build_contest_payload("x" * 1001, {"shipping_proof": ["doc_1"]}, "draft", settings)


def test_summary_at_the_limit_is_allowed(settings):
    payload = build_contest_payload("x" * 1000, {"shipping_proof": ["doc_1"]}, "draft", settings)
    assert len(payload["summary"]) == 1000


# ----------------------------------------------------------------------
# 5, 12. persistence and audit
# ----------------------------------------------------------------------

def test_draft_response_is_persisted(settings, approved_real_case):
    case, evidence, package, investigation = approved_real_case
    client = _mock_client()
    save_draft_to_razorpay(case, evidence, package, investigation,
                           actor="alice", settings=settings, client=client)

    attempts = get_contest_attempts(settings.paths.case_db, REAL_DISP)
    assert len(attempts) == 1
    assert attempts[0]["action"] == "draft"
    assert attempts[0]["succeeded"] is True
    assert attempts[0]["response"]["status"] == "under_review"
    assert attempts[0]["actor"] == "alice"


def test_audit_records_who_and_when_for_draft_and_submit(settings, approved_real_case):
    case, evidence, package, investigation = approved_real_case
    db = settings.paths.case_db
    client = _mock_client()

    save_draft_to_razorpay(case, evidence, package, investigation,
                           actor="alice", settings=settings, client=client)
    drafted = get_case(db, REAL_DISP)
    submit_contest(drafted, evidence, package, investigation, actor="bob",
                   human_confirmed=True, settings=settings, client=client)

    entries = get_audit_log(db, REAL_DISP)
    draft_entry = next(e for e in entries if e["action"] == "contest_draft_saved")
    submit_entry = next(e for e in entries if e["action"] == "contest_submitted")
    assert draft_entry["actor"] == "alice"
    assert submit_entry["actor"] == "bob"
    assert submit_entry["new_state"] == "SUBMITTED"
    assert submit_entry["timestamp"] > 0


def test_state_advances_only_through_legal_transitions(settings, approved_real_case):
    case, evidence, package, investigation = approved_real_case
    db = settings.paths.case_db
    client = _mock_client()

    assert get_case(db, REAL_DISP).case_state == "APPROVED"
    save_draft_to_razorpay(case, evidence, package, investigation,
                           actor="alice", settings=settings, client=client)
    assert get_case(db, REAL_DISP).case_state == "DRAFTED"

    submit_contest(get_case(db, REAL_DISP), evidence, package, investigation,
                   actor="alice", human_confirmed=True, settings=settings, client=client)
    assert get_case(db, REAL_DISP).case_state == "SUBMITTED"


# ----------------------------------------------------------------------
# 9, 10. failure safety and idempotency
# ----------------------------------------------------------------------

def test_failed_contest_call_leaves_case_state_untouched(settings, approved_real_case):
    from src.razorpay_client import RazorpayUnavailable
    case, evidence, package, investigation = approved_real_case
    db = settings.paths.case_db
    client = _mock_client()
    client.contest_dispute.side_effect = RazorpayUnavailable("network down")

    with pytest.raises(ContestError, match="unchanged"):
        save_draft_to_razorpay(case, evidence, package, investigation,
                               actor="alice", settings=settings, client=client)

    assert get_case(db, REAL_DISP).case_state == "APPROVED"


def test_failed_attempt_is_still_recorded_for_audit(settings, approved_real_case):
    from src.razorpay_client import RazorpayUnavailable
    case, evidence, package, investigation = approved_real_case
    client = _mock_client()
    client.contest_dispute.side_effect = RazorpayUnavailable("network down")

    with pytest.raises(ContestError):
        save_draft_to_razorpay(case, evidence, package, investigation,
                               actor="alice", settings=settings, client=client)

    attempts = get_contest_attempts(settings.paths.case_db, REAL_DISP)
    assert len(attempts) == 1
    assert attempts[0]["succeeded"] is False
    assert "network down" in attempts[0]["error"]


def test_retry_after_failure_does_not_reupload_documents(settings, approved_real_case):
    """Uploads are idempotent: a retry reuses the document ids already got."""
    from src.razorpay_client import RazorpayUnavailable
    case, evidence, package, investigation = approved_real_case
    client = _mock_client()
    client.contest_dispute.side_effect = RazorpayUnavailable("boom")

    with pytest.raises(ContestError):
        save_draft_to_razorpay(case, evidence, package, investigation,
                               actor="alice", settings=settings, client=client)
    uploads_first = client.upload_evidence_document.call_count
    assert uploads_first > 0

    client.contest_dispute.side_effect = None
    client.contest_dispute.return_value = {"id": REAL_DISP, "status": "under_review"}
    save_draft_to_razorpay(case, evidence, package, investigation,
                           actor="alice", settings=settings, client=client)

    assert client.upload_evidence_document.call_count == uploads_first  # no re-upload


def test_uploaded_document_ids_are_persisted(settings, approved_real_case):
    case, evidence, package, investigation = approved_real_case
    client = _mock_client()
    save_draft_to_razorpay(case, evidence, package, investigation,
                           actor="alice", settings=settings, client=client)

    stored = get_uploaded_documents(settings.paths.case_db, REAL_DISP)
    assert stored
    for record in stored.values():
        assert record["razorpay_document_id"].startswith("doc_")


def test_upload_failure_preserves_ids_already_obtained(settings, approved_real_case):
    from src.razorpay_client import RazorpayUnavailable
    case, evidence, package, investigation = approved_real_case
    client = _mock_client()
    calls = {"n": 0}

    def flaky(path, mime):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RazorpayUnavailable("upload failed midway")
        return {"id": f"doc_{calls['n']:014d}"}

    client.upload_evidence_document.side_effect = flaky
    with pytest.raises(ContestError):
        save_draft_to_razorpay(case, evidence, package, investigation,
                               actor="alice", settings=settings, client=client)

    # The first upload survived and will not be repeated.
    stored = get_uploaded_documents(settings.paths.case_db, REAL_DISP)
    assert len(stored) == 1
    assert get_case(settings.paths.case_db, REAL_DISP).case_state == "APPROVED"


def test_contest_with_no_documents_is_refused(settings, approved_real_case):
    case, evidence, package, investigation = approved_real_case
    client = _mock_client()
    with patch("src.contest_service.collect_uploadable_documents", return_value=[]):
        with pytest.raises(ContestError, match="at least one document"):
            save_draft_to_razorpay(case, evidence, package, investigation,
                                   actor="alice", settings=settings, client=client)
    assert get_case(settings.paths.case_db, REAL_DISP).case_state == "APPROVED"
