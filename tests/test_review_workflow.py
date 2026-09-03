"""Phase 6: deadline urgency, queue stats, and human decision recording."""

from __future__ import annotations

import time

import pytest

from src.config import load_settings
from src.database import (
    CaseRecord,
    get_audit_log,
    get_case,
    ingest_case,
    init_case_db,
    save_investigation,
)
from src.dispute_schema import DisputeEntity, IngestedCase, PaymentSummary
from src.investigation_schema import EvidenceCitation, InvestigationFailure, InvestigationResult
from src.review_workflow import (
    advance_to_review,
    build_case_summary,
    deadline_status,
    record_human_decision,
    summarise_queue,
)

HOUR = 3600


@pytest.fixture()
def settings(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abcdefghijklmn")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "x")
    return load_settings(require_razorpay=False)


@pytest.fixture()
def case_db(tmp_path):
    p = tmp_path / "cases.db"
    init_case_db(p)
    return p


def _seed(case_db, dispute_id="sim_disp_reviewtest01", respond_by=None, amount=1_500_000):
    respond_by = respond_by if respond_by is not None else int(time.time()) + 5 * 24 * HOUR
    dispute = DisputeEntity(
        id=dispute_id, payment_id="sim_pay_reviewtest01", amount=amount, currency="INR",
        amount_deducted=0, reason_code="goods_services_not_provided", respond_by=respond_by,
        status="open", phase="chargeback", created_at=1000,
    )
    payment = PaymentSummary(
        id="sim_pay_reviewtest01", order_id="sim_order_reviewte1", amount=amount,
        currency="INR", status="captured", method="card", captured=True,
        amount_refunded=0, refund_status=None, created_at=900,
    )
    return ingest_case(
        case_db,
        IngestedCase(dispute=dispute, payment=payment, source="simulated", is_simulated=True),
        actor="test",
    )


def _investigation(dispute_id="sim_disp_reviewtest01", classification="STRONG_CASE"):
    return InvestigationResult(
        dispute_id=dispute_id, classification=classification, confidence=0.9,
        executive_summary="summary", reason="reason",
        supporting_evidence=[EvidenceCitation("shipment", "ORD-1", "note")],
        missing_evidence=[], conflicting_evidence=[], recommended_action="CONTEST",
        risk_factors=[], investigation_timestamp=int(time.time()), model="test-model",
        is_simulated_case=True,
    )


# ----------------------------------------------------------------------
# deadline urgency (spec section 15) - thresholds must come from config
# ----------------------------------------------------------------------

def test_expired_deadline_is_detected(settings):
    now = int(time.time())
    status = deadline_status(now - HOUR, settings, now=now)
    assert status.urgency == "EXPIRED"
    assert status.is_expired
    assert "EXPIRED" in status.label


def test_critical_under_24_hours(settings):
    now = int(time.time())
    status = deadline_status(now + 6 * HOUR, settings, now=now)
    assert status.urgency == "CRITICAL"


def test_warning_under_72_hours(settings):
    now = int(time.time())
    status = deadline_status(now + 48 * HOUR, settings, now=now)
    assert status.urgency == "WARNING"


def test_normal_beyond_72_hours(settings):
    now = int(time.time())
    status = deadline_status(now + 10 * 24 * HOUR, settings, now=now)
    assert status.urgency == "NORMAL"


def test_thresholds_are_read_from_settings_not_hardcoded(settings):
    """The same timestamp must classify differently under different configured
    thresholds - proving deadline_status reads config rather than baking in 24/72.

    Note the tunables in config.py are dataclass defaults, so they are bound at
    import time: editing config.py and restarting is the supported way to change
    them. This constructs the config directly to test the read path itself.
    """
    from dataclasses import replace
    from src.config import DeadlineConfig

    now = int(time.time())
    at_30h = now + 30 * HOUR

    assert deadline_status(at_30h, settings, now=now).urgency == "WARNING"

    wider = replace(settings, deadlines=DeadlineConfig(critical_hours=48, warning_hours=96))
    assert deadline_status(at_30h, wider, now=now).urgency == "CRITICAL"

    narrower = replace(settings, deadlines=DeadlineConfig(critical_hours=2, warning_hours=8))
    assert deadline_status(at_30h, narrower, now=now).urgency == "NORMAL"


# ----------------------------------------------------------------------
# queue summary
# ----------------------------------------------------------------------

def test_summary_reflects_investigation_state(settings, case_db):
    case = _seed(case_db)
    summary = build_case_summary(case, case_db, settings)
    assert summary.classification is None
    assert not summary.investigation_failed
    assert summary.needs_review

    save_investigation(case_db, _investigation())
    summary = build_case_summary(get_case(case_db, case.dispute_id), case_db, settings)
    assert summary.classification == "STRONG_CASE"
    assert summary.confidence == 0.9


def test_failed_investigation_is_distinguished_from_no_investigation(settings, case_db):
    case = _seed(case_db)
    save_investigation(case_db, InvestigationFailure(
        dispute_id=case.dispute_id, failure_reason="AI_UNAVAILABLE", detail="down",
        investigation_timestamp=int(time.time()), attempts=1,
    ))
    summary = build_case_summary(case, case_db, settings)
    assert summary.investigation_failed
    assert summary.classification is None


def test_queue_stats_counts_classifications(settings, case_db):
    summaries = []
    for i, classification in enumerate(["STRONG_CASE", "WEAK_CASE", "NO_CASE", "STRONG_CASE"]):
        case = _seed(case_db, dispute_id=f"sim_disp_queuetest{i:03d}")
        save_investigation(case_db, _investigation(case.dispute_id, classification))
        summaries.append(build_case_summary(case, case_db, settings))

    stats = summarise_queue(summaries)
    assert stats.total == 4
    assert stats.strong == 2
    assert stats.weak == 1
    assert stats.no_case == 1
    assert stats.total_disputed_amount == 4 * 1_500_000


def test_queue_stats_on_empty_queue_does_not_crash(settings):
    stats = summarise_queue([])
    assert stats.total == 0
    assert stats.total_disputed_amount == 0


def test_expired_and_approaching_deadlines_are_counted(settings, case_db):
    now = int(time.time())
    summaries = []
    for i, offset in enumerate([-HOUR, 6 * HOUR, 48 * HOUR, 10 * 24 * HOUR]):
        case = _seed(case_db, dispute_id=f"sim_disp_deadline{i:04d}", respond_by=now + offset)
        summaries.append(build_case_summary(case, case_db, settings))
    stats = summarise_queue(summaries)
    assert stats.expired == 1
    assert stats.approaching_deadline == 2  # critical + warning


# ----------------------------------------------------------------------
# state progression + human decisions
# ----------------------------------------------------------------------

def test_advance_to_review_walks_legal_transitions_only(settings, case_db):
    case = _seed(case_db)
    assert case.case_state == "INGESTED"
    advanced = advance_to_review(case_db, case)
    assert advanced.case_state == "PENDING_HUMAN_REVIEW"

    states = [e["new_state"] for e in get_audit_log(case_db, case.dispute_id)]
    assert states == ["INGESTED", "ANALYZING", "ANALYSIS_COMPLETE", "PENDING_HUMAN_REVIEW"]


def test_advance_is_idempotent_for_already_reviewed_cases(settings, case_db):
    case = _seed(case_db)
    advance_to_review(case_db, case)
    current = get_case(case_db, case.dispute_id)
    again = advance_to_review(case_db, current)
    assert again.case_state == "PENDING_HUMAN_REVIEW"


def test_human_approve_records_state_and_audit(settings, case_db):
    case = _seed(case_db)
    advance_to_review(case_db, case)
    record_human_decision(
        case_db, case.dispute_id, "APPROVE", reviewer="alice",
        reason="delivery proof is solid", ai_classification="STRONG_CASE",
    )
    assert get_case(case_db, case.dispute_id).case_state == "APPROVED"
    last = get_audit_log(case_db, case.dispute_id)[-1]
    assert last["actor"] == "alice"
    assert last["action"] == "human_approve"
    assert "STRONG_CASE" in last["reason"]


def test_human_reject_overrules_the_ai(settings, case_db):
    case = _seed(case_db)
    advance_to_review(case_db, case)
    record_human_decision(
        case_db, case.dispute_id, "REJECT", reviewer="bob",
        reason="not worth contesting", ai_classification="STRONG_CASE",
    )
    assert get_case(case_db, case.dispute_id).case_state == "OVERRULED"
    last = get_audit_log(case_db, case.dispute_id)[-1]
    assert last["action"] == "human_reject"


def test_audit_records_both_ai_recommendation_and_human_decision(settings, case_db):
    """A reader must be able to see where a human overruled the model."""
    case = _seed(case_db)
    advance_to_review(case_db, case)
    record_human_decision(
        case_db, case.dispute_id, "REJECT", reviewer="carol",
        reason="courier record looks unreliable", ai_classification="STRONG_CASE",
    )
    reason = get_audit_log(case_db, case.dispute_id)[-1]["reason"]
    assert "AI recommended STRONG_CASE" in reason
    assert "human REJECT" in reason


def test_request_further_review_does_not_change_state(settings, case_db):
    """There is no state for 'needs more work' in the spec's machine, so the
    case must stay put and only the audit log records the request."""
    case = _seed(case_db)
    advance_to_review(case_db, case)
    before = get_case(case_db, case.dispute_id).case_state

    record_human_decision(
        case_db, case.dispute_id, "REQUEST_FURTHER_REVIEW", reviewer="dave",
        reason="need the courier's last-mile scan", ai_classification="WEAK_CASE",
    )

    assert get_case(case_db, case.dispute_id).case_state == before == "PENDING_HUMAN_REVIEW"
    last = get_audit_log(case_db, case.dispute_id)[-1]
    assert last["action"] == "human_request_further_review"
    assert "last-mile" in last["reason"]


def test_cannot_approve_a_case_that_skipped_review(settings, case_db):
    """The state machine still guards: no jumping straight to APPROVED."""
    from src.database import InvalidStateTransition
    case = _seed(case_db)  # still INGESTED
    with pytest.raises(InvalidStateTransition):
        record_human_decision(
            case_db, case.dispute_id, "APPROVE", reviewer="eve", reason="rush",
        )


# ----------------------------------------------------------------------
# human-readable labels and the workflow stepper
# ----------------------------------------------------------------------

def test_case_state_label_covers_every_real_state():
    from src.database import ALLOWED_TRANSITIONS
    from src.review_workflow import case_state_label
    for state in ALLOWED_TRANSITIONS:
        label = case_state_label(state)
        assert label and label != state  # must actually translate, not echo


def test_case_state_label_falls_back_gracefully_for_unknown_state():
    from src.review_workflow import case_state_label
    assert case_state_label("SOME_FUTURE_STATE") == "Some Future State"


def test_reason_code_label_translates_known_codes():
    from src.review_workflow import reason_code_label
    label = reason_code_label("goods_services_not_provided")
    assert "provided" in label.lower()
    assert label != "goods_services_not_provided"


def test_reason_code_label_degrades_safely_for_unknown_codes():
    """reason_code has no documented closed enum (NOTES.md), so a code we've
    never seen must still produce something readable, never crash."""
    from src.review_workflow import reason_code_label
    label = reason_code_label("some_brand_new_reason_code")
    assert label  # did not raise, did not return empty
    assert "_" not in label


def test_workflow_progress_marks_exactly_one_current_step():
    from src.review_workflow import workflow_progress
    for state in ["INGESTED", "ANALYZING", "PENDING_HUMAN_REVIEW", "APPROVED", "SUBMITTED"]:
        steps = workflow_progress(state)
        current = [s for s in steps if s.is_current]
        assert len(current) == 1
        assert current[0].state == state


def test_workflow_progress_marks_earlier_steps_complete():
    from src.review_workflow import workflow_progress
    steps = workflow_progress("APPROVED")
    complete_states = {s.state for s in steps if s.is_complete}
    assert complete_states == {"INGESTED", "ANALYZING", "ANALYSIS_COMPLETE", "PENDING_HUMAN_REVIEW"}


def test_workflow_progress_overruled_shows_where_it_stopped():
    """A rejected case reached PENDING_HUMAN_REVIEW - that must show as
    reached, with everything after it marked stopped, not silently omitted."""
    from src.review_workflow import workflow_progress
    steps = workflow_progress("OVERRULED")
    by_state = {s.state: s for s in steps}
    assert by_state["PENDING_HUMAN_REVIEW"].is_complete
    assert not any(s.is_current for s in steps)
    assert by_state["APPROVED"].is_stopped
    assert by_state["DRAFTED"].is_stopped
    assert by_state["SUBMITTED"].is_stopped
