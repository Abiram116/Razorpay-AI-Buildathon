"""Real-vs-simulated id boundary, and rejection of out-of-spec values."""

import pytest

from src.dispute_schema import (
    DisputeValidationError,
    parse_dispute,
    parse_payment,
    parse_webhook_envelope,
)

REAL_DISPUTE = {
    "id": "disp_EsIAlDcoUr8CaQ", "entity": "dispute", "payment_id": "pay_EFtmUsbwpXwBHI",
    "amount": 39000, "currency": "INR", "amount_deducted": 0,
    "reason_code": "processed_invalid_expired_card", "respond_by": 1590431400,
    "status": "open", "phase": "chargeback", "created_at": 1589907957,
}


def test_real_dispute_parses():
    d = parse_dispute(REAL_DISPUTE)
    assert d.id == "disp_EsIAlDcoUr8CaQ"


def test_simulated_id_rejected_unless_allowed():
    sim = {**REAL_DISPUTE, "id": "sim_disp_abc123", "payment_id": "sim_pay_abc123"}
    with pytest.raises(DisputeValidationError):
        parse_dispute(sim)
    parse_dispute(sim, allow_simulated=True)  # should not raise


def test_real_id_rejected_when_malformed():
    bad = {**REAL_DISPUTE, "id": "disp_tooshort"}
    with pytest.raises(DisputeValidationError):
        parse_dispute(bad)


def test_unknown_status_rejected_not_coerced():
    bad = {**REAL_DISPUTE, "status": "resolved_favorably"}  # not a documented value
    with pytest.raises(DisputeValidationError):
        parse_dispute(bad)


def test_unknown_phase_rejected():
    bad = {**REAL_DISPUTE, "phase": "escalated"}
    with pytest.raises(DisputeValidationError):
        parse_dispute(bad)


def test_negative_or_zero_amount_rejected():
    for amt in (0, -100):
        with pytest.raises(DisputeValidationError):
            parse_dispute({**REAL_DISPUTE, "amount": amt})


def test_missing_required_field_rejected():
    incomplete = {k: v for k, v in REAL_DISPUTE.items() if k != "respond_by"}
    with pytest.raises(DisputeValidationError):
        parse_dispute(incomplete)


def test_envelope_rejects_non_event_entity():
    with pytest.raises(DisputeValidationError):
        parse_webhook_envelope({"entity": "not_event", "event": "payment.dispute.created"})


def test_envelope_rejects_unrecognised_event():
    with pytest.raises(DisputeValidationError):
        parse_webhook_envelope({"entity": "event", "event": "totally.made.up", "payload": {}})
