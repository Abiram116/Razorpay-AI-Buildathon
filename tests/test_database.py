"""State machine legality, audit trail, and webhook idempotency at the DB layer."""

import pytest

from src import database
from src.dispute_schema import DataSource, DisputeEntity, IngestedCase, PaymentSummary


def _case(dispute_id="disp_AAAAAAAAAAAAAA", source: DataSource = "razorpay_webhook") -> IngestedCase:
    dispute = DisputeEntity(
        id=dispute_id, payment_id="pay_BBBBBBBBBBBBBB", amount=25000, currency="INR",
        amount_deducted=0, reason_code="goods_services_not_provided", respond_by=9999999999,
        status="open", phase="chargeback", created_at=1000,
    )
    payment = PaymentSummary(
        id="pay_BBBBBBBBBBBBBB", order_id="order_CCCCCCCCCCCCCC", amount=25000, currency="INR",
        status="captured", method="card", captured=True, amount_refunded=0,
        refund_status=None, created_at=900,
    )
    return IngestedCase(dispute=dispute, payment=payment, source=source, is_simulated=source == "simulated")


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "cases.db"
    database.init_case_db(p)
    return p


def test_ingest_creates_case_at_ingested_state(db_path):
    record = database.ingest_case(db_path, _case(), actor="test")
    assert record.case_state == "INGESTED"
    log = database.get_audit_log(db_path, record.dispute_id)
    assert log[0]["action"] == "ingest"
    assert log[0]["new_state"] == "INGESTED"


def test_legal_transition_succeeds(db_path):
    record = database.ingest_case(db_path, _case(), actor="test")
    updated = database.transition_case_state(
        db_path, record.dispute_id, "ANALYZING", actor="ai_agent", action="start_analysis"
    )
    assert updated.case_state == "ANALYZING"


def test_illegal_transition_is_rejected(db_path):
    record = database.ingest_case(db_path, _case(), actor="test")
    with pytest.raises(database.InvalidStateTransition):
        # INGESTED -> SUBMITTED skips the whole workflow; must be refused.
        database.transition_case_state(
            db_path, record.dispute_id, "SUBMITTED", actor="test", action="skip"
        )


def test_terminal_states_have_no_outgoing_transitions(db_path):
    record = database.ingest_case(db_path, _case(), actor="test")
    database.transition_case_state(db_path, record.dispute_id, "ANALYZING", "a", "x")
    database.transition_case_state(db_path, record.dispute_id, "ANALYSIS_COMPLETE", "a", "x")
    database.transition_case_state(db_path, record.dispute_id, "PENDING_HUMAN_REVIEW", "a", "x")
    database.transition_case_state(db_path, record.dispute_id, "OVERRULED", "human", "overrule")
    with pytest.raises(database.InvalidStateTransition):
        database.transition_case_state(db_path, record.dispute_id, "APPROVED", "human", "x")


def test_webhook_receipt_idempotency(db_path):
    first = database.record_webhook_receipt(db_path, "hash123", "payment.dispute.created", "disp_X", True)
    second = database.record_webhook_receipt(db_path, "hash123", "payment.dispute.created", "disp_X", True)
    assert first is True
    assert second is False


def test_simulated_case_flagged_correctly(db_path):
    record = database.ingest_case(db_path, _case(dispute_id="disp_SIMTEST0000AA", source="simulated"), actor="sim")
    assert record.is_simulated is True
    assert record.source == "simulated"
