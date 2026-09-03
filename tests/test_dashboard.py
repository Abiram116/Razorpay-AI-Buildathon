"""Phase 6: the dashboard itself, driven through Streamlit's AppTest.

These run the real app script against a temporary database. No live Groq or
Razorpay call is made: the investigation agent is stubbed where a test needs
a result, and most tests use pre-saved investigations.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from src.config import Paths, load_settings
from src.database import (
    get_audit_log,
    get_case,
    get_latest_investigation,
    ingest_case,
    init_case_db,
    save_investigation,
)
from src.dispute_schema import DisputeEntity, IngestedCase, PaymentSummary
from src.investigation_schema import EvidenceCitation, InvestigationFailure, InvestigationResult
from src.merchant_db import (
    Communication,
    EvidenceDocument,
    Order,
    Policy,
    Refund,
    Shipment,
    init_merchant_db,
    insert_communication,
    insert_document,
    insert_order,
    insert_policy,
    insert_refund,
    insert_shipment,
)
from src.review_workflow import advance_to_review

APP = str(Path(__file__).resolve().parent.parent / "dashboard" / "app.py")
DISP = "sim_disp_dashboardtest"
PAY = "sim_pay_dashboardtes"
HOUR = 3600


@pytest.fixture()
def demo_env(tmp_path, monkeypatch):
    """A populated temp environment the dashboard will read."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abcdefghijklmn")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "x")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake")

    case_db, merchant_db = tmp_path / "cases.db", tmp_path / "merchant.db"
    init_case_db(case_db)
    init_merchant_db(merchant_db)

    respond_by = int(time.time()) + 5 * 24 * HOUR
    dispute = DisputeEntity(
        id=DISP, payment_id=PAY, amount=1_500_000, currency="INR", amount_deducted=0,
        reason_code="goods_services_not_provided", respond_by=respond_by,
        status="open", phase="chargeback", created_at=1000,
    )
    payment = PaymentSummary(
        id=PAY, order_id="sim_order_dashboard", amount=1_500_000, currency="INR",
        status="captured", method="card", captured=True, amount_refunded=0,
        refund_status=None, created_at=900,
    )
    ingest_case(case_db, IngestedCase(dispute=dispute, payment=payment,
                                       source="simulated", is_simulated=True), actor="test")

    insert_order(merchant_db, Order(
        "ORD-D1", "sim_order_dashboard", PAY, "CUST-D", "Smartphone", "physical",
        1_500_000, "INR", 1000, "fulfilled", "Addr", "Addr", True,
    ))
    insert_shipment(merchant_db, Shipment(
        "ORD-D1", "TRK-D", "BlueDart", 1100, 1200, "delivered", "Bengaluru",
        "Signed by recipient",
    ))
    insert_communication(merchant_db, Communication(
        "ORD-D1", "CUST-D", 1300, "chat", "I received the package", "inbound",
    ))
    insert_refund(merchant_db, Refund("ORD-D1", PAY, False, "none", None, None, None))
    insert_document(merchant_db, EvidenceDocument(
        "ORD-D1", "shipping_proof", "pod.txt", "Signed proof of delivery",
    ))
    insert_policy(merchant_db, Policy("refund_policy", "v1", 500, "7 day returns"))

    real_load = load_settings

    def patched(require_razorpay: bool = True):
        s = real_load(require_razorpay=False)
        object.__setattr__(s, "paths", Paths(
            merchant_db=merchant_db, case_db=case_db, generated_docs=tmp_path / "generated",
        ))
        return s

    monkeypatch.setattr("src.config.load_settings", patched)
    monkeypatch.setattr("dashboard.app.load_settings", patched, raising=False)
    return case_db, merchant_db, patched


def _investigation(classification="STRONG_CASE", **overrides):
    defaults = dict(
        dispute_id=DISP, classification=classification, confidence=0.94,
        executive_summary="Delivery is proven and the customer confirmed receipt.",
        reason="Courier records show signed delivery.",
        supporting_evidence=[
            EvidenceCitation("shipment", "ORD-D1", "signed delivery"),
            EvidenceCitation("order", "ORD-D1", "order fulfilled"),
        ],
        missing_evidence=[], conflicting_evidence=[], recommended_action="CONTEST",
        risk_factors=[], investigation_timestamp=int(time.time()), model="test-model",
        is_simulated_case=True,
    )
    defaults.update(overrides)
    return InvestigationResult(**defaults)


def _run(patched_loader) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    with patch("src.config.load_settings", patched_loader):
        at.run()
    return at


def _all_text(at: AppTest) -> str:
    parts = []
    for collection in (at.markdown, at.caption, at.warning, at.error,
                       at.info, at.success, at.title, at.header, at.subheader):
        parts.extend(str(e.value) for e in collection)
    return "\n".join(parts)


# ----------------------------------------------------------------------
# loading and overview
# ----------------------------------------------------------------------

def test_dashboard_loads_without_error(demo_env):
    _, _, loader = demo_env
    at = _run(loader)
    assert not at.exception
    assert "AI Chargeback Defense Manager" in at.title[0].value


def test_overview_metrics_render(demo_env):
    _, _, loader = demo_env
    at = _run(loader)
    labels = {m.label for m in at.metric}
    assert "Total disputes" in labels
    assert "Awaiting human review" in labels
    assert "🟢 Strong case" in labels
    assert "🔴 No case" in labels


def test_queue_lists_the_case_and_allows_selection(demo_env):
    _, _, loader = demo_env
    at = _run(loader)
    assert at.selectbox
    # AppTest reports options with format_func already applied, so the raw id
    # appears inside the rendered label rather than as the option itself.
    assert any(DISP in option for option in at.selectbox[0].options)
    assert any("INR 15,000.00" in option for option in at.selectbox[0].options)


def test_product_purpose_is_stated_up_front(demo_env):
    """A judge should understand what this is within seconds."""
    _, _, loader = demo_env
    at = _run(loader)
    text = _all_text(at)
    assert "disputes" in text.lower()
    assert "human" in text.lower()


# ----------------------------------------------------------------------
# provenance - simulated data must never look real
# ----------------------------------------------------------------------

def test_simulated_case_is_labelled_as_simulated(demo_env):
    _, _, loader = demo_env
    at = _run(loader)
    text = _all_text(at)
    assert "SIMULATED" in text
    assert "Razorpay provides no API to create a test dispute" in text


# ----------------------------------------------------------------------
# investigation display
# ----------------------------------------------------------------------

def test_uninvestigated_case_is_automatically_investigated_without_a_button(demo_env):
    """A reviewer's job is to decide, not to remember to click 'investigate'
    on every case - opening an uninvestigated case must trigger the AI
    automatically, with no manual step in between."""
    case_db, _, loader = demo_env
    mock_result = _investigation()
    with patch("src.investigation_agent.investigate_dispute", return_value=mock_result) as mock_investigate:
        _run(loader)

    mock_investigate.assert_called_once()
    stored = get_latest_investigation(case_db, DISP)
    assert stored is not None and stored["succeeded"] is True
    assert stored["classification"] == "STRONG_CASE"
    # auto-investigation must also advance the case out of INGESTED, so the
    # workflow stepper and "awaiting review" state are consistent with what
    # is actually on screen (see BUILD_LOG 2026-09-03 stepper/advance fix).
    assert get_case(case_db, DISP).case_state == "PENDING_HUMAN_REVIEW"


def test_already_investigated_case_is_not_re_investigated(demo_env):
    """Investigating twice would waste a Groq call and money on every rerun
    of a case that already has a saved result."""
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation())
    with patch("src.investigation_agent.investigate_dispute") as mock_investigate:
        _run(loader)
    mock_investigate.assert_not_called()


def test_no_manual_investigate_button_exists_in_the_ui():
    """Regression guard: there must be no 'click to investigate' step at all."""
    source = Path(APP).read_text()
    assert "Run AI investigation" not in source


def test_investigation_result_is_displayed_with_confidence(demo_env):
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation())
    at = _run(loader)
    text = _all_text(at)
    assert "STRONG CASE" in text
    assert "94%" in text
    assert "RECOMMENDATION ONLY" in text


def test_ai_verdict_is_framed_as_recommendation_not_decision(demo_env):
    """The human-in-the-loop boundary must be explicit on screen."""
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation())
    at = _run(loader)
    text = _all_text(at)
    assert "RECOMMENDATION ONLY" in text
    assert "recommendation, not a decision" in text
    assert "not executed" in text.lower()


def test_missing_and_conflicting_evidence_are_shown(demo_env):
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation(
        classification="WEAK_CASE", recommended_action="MANUAL_REVIEW",
        missing_evidence=["recipient signature"],
        conflicting_evidence=["status says delivered but no signature"],
    ))
    at = _run(loader)
    text = _all_text(at)
    assert "recipient signature" in text
    assert "status says delivered but no signature" in text


def test_failed_investigation_shows_no_recommendation(demo_env):
    """A failure must never be rendered as a finding."""
    case_db, _, loader = demo_env
    save_investigation(case_db, InvestigationFailure(
        dispute_id=DISP, failure_reason="AI_UNAVAILABLE",
        detail="AI investigation unavailable - manual review required.",
        investigation_timestamp=int(time.time()), attempts=1,
    ))
    at = _run(loader)
    text = _all_text(at)
    assert "AI_UNAVAILABLE" in text
    assert "manual review" in text.lower()
    assert "STRONG CASE" not in text


# ----------------------------------------------------------------------
# evidence display and citation traceability
# ----------------------------------------------------------------------

def test_merchant_evidence_is_displayed(demo_env):
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation())
    at = _run(loader)
    assert at.tabs
    tab_labels = _all_text(at)
    assert "Signed proof of delivery" in tab_labels or "shipping_proof" in tab_labels


def test_citations_are_shown_with_their_source_records(demo_env):
    """No AI claim may be displayed without the record behind it."""
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation())
    at = _run(loader)
    text = _all_text(at)
    assert "shipment:ORD-D1" in text
    assert "signed delivery" in text


def test_evidence_categories_are_listed(demo_env):
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation())
    at = _run(loader)
    text = _all_text(at)
    assert "shipping_proof" in text
    assert "only those with real records behind them" in text


# ----------------------------------------------------------------------
# NO_CASE guardrail
# ----------------------------------------------------------------------

def test_no_case_refuses_package_generation_in_the_ui(demo_env):
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation(
        classification="NO_CASE", recommended_action="DO_NOT_CONTEST",
        supporting_evidence=[],
    ))
    at = _run(loader)
    text = _all_text(at)
    assert "not building an evidence package" in text.lower()
    assert "NO CASE" in text


def test_no_case_override_requires_explicit_confirmation(demo_env):
    """The override button must be disabled until a human ticks the box."""
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation(
        classification="NO_CASE", recommended_action="DO_NOT_CONTEST",
        supporting_evidence=[],
    ))
    at = _run(loader)
    assert at.checkbox, "an explicit confirmation checkbox must exist"
    assert not at.checkbox[0].value

    override_buttons = [b for b in at.button if "Override" in b.label]
    assert override_buttons
    assert override_buttons[0].disabled is True


# ----------------------------------------------------------------------
# human review
# ----------------------------------------------------------------------

def test_human_review_offers_approve_reject_and_further_review(demo_env):
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation())
    at = _run(loader)
    labels = " ".join(b.label for b in at.button)
    assert "Approve" in labels
    assert "Reject" in labels
    assert "further review" in labels


def test_approving_records_state_and_audit_entry(demo_env):
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation())
    at = _run(loader)

    approve = next(b for b in at.button if "Approve" in b.label)
    with patch("src.config.load_settings", loader):
        approve.click().run()

    assert get_case(case_db, DISP).case_state == "APPROVED"
    actions = [e["action"] for e in get_audit_log(case_db, DISP)]
    assert "human_approve" in actions


def test_rejecting_overrules_the_ai_and_is_audited(demo_env):
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation())
    at = _run(loader)

    reject = next(b for b in at.button if "Reject" in b.label)
    with patch("src.config.load_settings", loader):
        reject.click().run()

    assert get_case(case_db, DISP).case_state == "OVERRULED"
    entry = get_audit_log(case_db, DISP)[-1]
    assert entry["action"] == "human_reject"
    assert "AI recommended STRONG_CASE" in entry["reason"]


# ----------------------------------------------------------------------
# the central safety property
# ----------------------------------------------------------------------

def test_dashboard_never_submits_to_razorpay(demo_env):
    """No code path in the UI may call the Razorpay contest API."""
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation())

    with patch("src.razorpay_client.RazorpayClient.contest_dispute") as contest, \
         patch("src.razorpay_client.RazorpayClient.upload_evidence_document") as upload:
        at = _run(loader)
        approve = next(b for b in at.button if "Approve" in b.label)
        with patch("src.config.load_settings", loader):
            approve.click().run()

    contest.assert_not_called()
    upload.assert_not_called()


def test_approved_case_shows_a_draft_not_a_submission(demo_env):
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation())
    case = get_case(case_db, DISP)
    advance_to_review(case_db, case)
    from src.review_workflow import record_human_decision
    record_human_decision(case_db, DISP, "APPROVE", reviewer="t", reason="ok",
                          ai_classification="STRONG_CASE")

    at = _run(loader)
    text = _all_text(at)
    assert "DRAFT" in text
    assert "NOT SUBMITTED" in text


def test_simulated_case_shows_submission_blocked_in_the_ui(demo_env):
    """The demo case is simulated, so the UI must surface the backend block
    and offer no submit control at all."""
    case_db, _, loader = demo_env
    save_investigation(case_db, _investigation())
    case = get_case(case_db, DISP)
    advance_to_review(case_db, case)
    from src.review_workflow import record_human_decision
    record_human_decision(case_db, DISP, "APPROVE", reviewer="t", reason="ok",
                          ai_classification="STRONG_CASE")

    at = _run(loader)
    text = _all_text(at)
    assert "Submission blocked" in text
    assert "SIMULATED" in text
    assert "assert_submittable" in text  # proves the block lives in code, not just UI copy
    assert not [b for b in at.button if "Submit contest" in b.label]


def test_dashboard_never_calls_contest_dispute_directly():
    """The UI may only reach the contest/submit API through contest_service,
    which carries the simulated-dispute block and the human-confirmation
    gate. Calling contest_dispute directly would bypass both.

    Document upload is a different matter: `POST /v1/documents` cannot
    submit or contest anything and needs no existing dispute, so the
    standalone integration-proof panel (render_integration_proof) is allowed
    to call it directly - it demonstrates real API connectivity without
    touching any case's state or the safety-gated contest/submit path.
    """
    source = Path(APP).read_text()
    assert "contest_dispute" not in source


def test_live_upload_proof_never_touches_contest_or_submit():
    """The one place the dashboard calls RazorpayClient directly must be
    confined to the upload-only proof panel, never near a contest call."""
    source = Path(APP).read_text()
    assert "RazorpayClient" in source  # the live proof panel legitimately uses it
    proof_fn = source[source.index("def render_integration_proof"):source.index("def _styles_for_proof")]
    assert "upload_evidence_document" in proof_fn
    assert "contest_dispute" not in proof_fn
    assert "submit_contest" not in proof_fn


def test_dashboard_only_submits_with_explicit_human_confirmation():
    """Every submit_contest call in the UI must pass human_confirmed=True,
    and it must sit behind a confirmation checkbox."""
    import re
    source = Path(APP).read_text()
    for match in re.finditer(r"submit_contest\((.*?)\)", source, re.S):
        assert "human_confirmed=True" in match.group(1)
    assert "confirm_submit_" in source  # the gating checkbox key


def test_live_upload_proof_button_calls_upload_when_clicked(demo_env):
    """Clicking the proof button must call RazorpayClient.upload_evidence_document
    - mocked here so the test suite never makes a real network call - and
    display the returned document id."""
    _, _, loader = demo_env
    fake_response = {"id": "doc_TestProof00001", "entity": "document"}

    with patch("src.razorpay_client.RazorpayClient.upload_evidence_document",
              return_value=fake_response) as mock_upload:
        at = _run(loader)
        expanders = [e for e in at.expander if "actually live here" in e.label]
        assert expanders, "integration proof panel must be present"
        buttons = [b for b in at.button if "Upload a real evidence document" in b.label]
        assert buttons
        with patch("src.config.load_settings", loader):
            buttons[0].click().run()

    mock_upload.assert_called_once()
    args, kwargs = mock_upload.call_args
    assert args[0].endswith(".pdf")
    assert args[1] == "application/pdf"
