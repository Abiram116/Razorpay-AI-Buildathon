"""Phase 3: schema integrity, seeding, retrieval, relationships, fail-safety."""

import pytest

from src.dispute_schema import generate_simulated_id
from src.merchant_db import (
    Communication,
    EvidenceDocument,
    MerchantDataError,
    Order,
    Policy,
    Refund,
    Shipment,
    get_active_policies,
    get_case_evidence,
    get_communications,
    get_documents,
    get_order,
    get_order_by_payment_id,
    get_order_by_razorpay_order_id,
    get_refund,
    get_shipment,
    init_merchant_db,
    insert_communication,
    insert_document,
    insert_order,
    insert_policy,
    insert_refund,
    insert_shipment,
)
from src.merchant_seed_data import get_policies, get_scenarios


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "merchant.db"
    init_merchant_db(p)
    return p


def _order(order_id="ORD-T1", payment_id=None, razorpay_order_id=None, **overrides) -> Order:
    payment_id = payment_id or generate_simulated_id("pay")
    razorpay_order_id = razorpay_order_id or generate_simulated_id("order")
    defaults = dict(
        merchant_order_id=order_id, razorpay_order_id=razorpay_order_id, payment_id=payment_id,
        customer_id="CUST-T1", product="Widget", product_type="physical", amount=10_000,
        currency="INR", order_timestamp=1_000_000, order_status="fulfilled",
        shipping_address="Somewhere", billing_address="Somewhere", is_simulated=True,
    )
    defaults.update(overrides)
    return Order(**defaults)


# ----------------------------------------------------------------------
# schema / validation
# ----------------------------------------------------------------------

def test_schema_creates_all_expected_tables(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    expected = {"orders", "shipments", "customer_communications", "refunds", "policies", "documents"}
    assert expected.issubset(tables)


def test_reinitializing_schema_is_idempotent(db_path):
    init_merchant_db(db_path)  # must not raise on a second call
    insert_order(db_path, _order())
    assert get_order(db_path, "ORD-T1") is not None


def test_malformed_razorpay_order_id_rejected(db_path):
    with pytest.raises(MerchantDataError):
        insert_order(db_path, _order(razorpay_order_id="order_not_real_and_too_short"))


def test_malformed_payment_id_rejected(db_path):
    with pytest.raises(MerchantDataError):
        insert_order(db_path, _order(payment_id="totally-made-up"))


def test_simulated_ids_are_accepted(db_path):
    insert_order(db_path, _order())  # uses sim_ ids by default - must not raise
    assert get_order(db_path, "ORD-T1") is not None


def test_invalid_product_type_rejected_by_check_constraint(db_path):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        insert_order(db_path, _order(product_type="edible"))


def test_negative_amount_rejected_by_check_constraint(db_path):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        insert_order(db_path, _order(amount=-500))


# ----------------------------------------------------------------------
# retrieval
# ----------------------------------------------------------------------

def test_get_order_by_all_three_keys(db_path):
    pay_id, rzp_order_id = generate_simulated_id("pay"), generate_simulated_id("order")
    insert_order(db_path, _order(payment_id=pay_id, razorpay_order_id=rzp_order_id))

    assert get_order(db_path, "ORD-T1").merchant_order_id == "ORD-T1"
    assert get_order_by_payment_id(db_path, pay_id).merchant_order_id == "ORD-T1"
    assert get_order_by_razorpay_order_id(db_path, rzp_order_id).merchant_order_id == "ORD-T1"


def test_unknown_order_returns_none_not_an_exception(db_path):
    assert get_order(db_path, "ORD-DOES-NOT-EXIST") is None
    assert get_order_by_payment_id(db_path, generate_simulated_id("pay")) is None


def test_policy_versioning_returns_latest_only(db_path):
    insert_policy(db_path, Policy("refund_policy", "v1", effective_from=100, content="old text"))
    insert_policy(db_path, Policy("refund_policy", "v2", effective_from=200, content="new text"))
    policies = get_active_policies(db_path)
    refund_policies = [p for p in policies if p.policy_type == "refund_policy"]
    assert len(refund_policies) == 1
    assert refund_policies[0].version == "v2"
    assert refund_policies[0].content == "new text"


# ----------------------------------------------------------------------
# relationships - records must not cross-contaminate between orders
# ----------------------------------------------------------------------

def test_communications_and_documents_scoped_to_their_own_order(db_path):
    insert_order(db_path, _order(order_id="ORD-A"))
    insert_order(db_path, _order(order_id="ORD-B"))
    insert_communication(db_path, Communication("ORD-A", "CUST-A", 100, "email", "msg A", "inbound"))
    insert_communication(db_path, Communication("ORD-B", "CUST-B", 100, "email", "msg B", "inbound"))
    insert_document(db_path, EvidenceDocument("ORD-A", "shipping_proof", "a.txt", "doc A"))
    insert_document(db_path, EvidenceDocument("ORD-B", "billing_proof", "b.txt", "doc B"))

    comms_a = get_communications(db_path, "ORD-A")
    docs_a = get_documents(db_path, "ORD-A")
    assert [c.message for c in comms_a] == ["msg A"]
    assert [d.filename for d in docs_a] == ["a.txt"]


def test_communications_ordered_chronologically(db_path):
    insert_order(db_path, _order())
    insert_communication(db_path, Communication("ORD-T1", "CUST-T1", 300, "chat", "third", "inbound"))
    insert_communication(db_path, Communication("ORD-T1", "CUST-T1", 100, "chat", "first", "inbound"))
    insert_communication(db_path, Communication("ORD-T1", "CUST-T1", 200, "chat", "second", "outbound"))
    messages = [c.message for c in get_communications(db_path, "ORD-T1")]
    assert messages == ["first", "second", "third"]


def test_order_without_shipment_or_refund_returns_none_cleanly(db_path):
    insert_order(db_path, _order(product_type="digital"))
    assert get_shipment(db_path, "ORD-T1") is None
    assert get_refund(db_path, "ORD-T1") is None
    assert get_documents(db_path, "ORD-T1") == []


# ----------------------------------------------------------------------
# the Phase 4/5 aggregation entrypoint
# ----------------------------------------------------------------------

def test_case_evidence_bundles_everything_for_a_full_scenario(db_path):
    pay_id = generate_simulated_id("pay")
    insert_order(db_path, _order(payment_id=pay_id))
    insert_shipment(db_path, Shipment("ORD-T1", "TRK1", "BlueDart", 100, 200, "delivered", "City", "yes"))
    insert_communication(db_path, Communication("ORD-T1", "CUST-T1", 250, "chat", "got it", "inbound"))
    insert_refund(db_path, Refund("ORD-T1", pay_id, False, "none", None, None, None))
    insert_document(db_path, EvidenceDocument("ORD-T1", "shipping_proof", "f.txt", "d"))
    insert_policy(db_path, Policy("refund_policy", "v1", 50, "policy text"))

    evidence = get_case_evidence(db_path, payment_id=pay_id)
    assert evidence is not None
    assert evidence.order.merchant_order_id == "ORD-T1"
    assert evidence.shipment.delivery_status == "delivered"
    assert len(evidence.communications) == 1
    assert evidence.refund.refund_status == "none"
    assert len(evidence.documents) == 1
    assert len(evidence.policies) == 1


def test_case_evidence_for_digital_order_has_no_shipment_but_is_not_an_error(db_path):
    pay_id = generate_simulated_id("pay")
    insert_order(db_path, _order(payment_id=pay_id, product_type="digital"))
    evidence = get_case_evidence(db_path, payment_id=pay_id)
    assert evidence is not None
    assert evidence.shipment is None
    assert evidence.communications == []


def test_case_evidence_for_unknown_payment_returns_none_not_fabricated_data(db_path):
    """Fail-safe: no merchant record at all must surface as 'we don't know',
    never as an empty-but-present evidence bundle the AI might read as
    'the merchant confirmed nothing exists', which is a different claim."""
    assert get_case_evidence(db_path, payment_id=generate_simulated_id("pay")) is None


# ----------------------------------------------------------------------
# seed data itself
# ----------------------------------------------------------------------

def test_seed_scenarios_cover_both_defensible_and_indefensible_cases():
    scenarios = get_scenarios()
    strengths = {s.expected_strength for s in scenarios}
    assert "STRONG_CASE" in strengths
    assert "NO_CASE" in strengths
    assert len(scenarios) >= 5


def test_seed_scenarios_have_unique_order_and_payment_ids():
    scenarios = get_scenarios()
    order_ids = [s.order.merchant_order_id for s in scenarios]
    payment_ids = [s.order.payment_id for s in scenarios]
    assert len(order_ids) == len(set(order_ids))
    assert len(payment_ids) == len(set(payment_ids))


def test_seed_scenarios_insert_cleanly_into_a_fresh_db(db_path):
    for policy in get_policies():
        insert_policy(db_path, policy)
    for scenario in get_scenarios():
        insert_order(db_path, scenario.order)
        if scenario.shipment:
            insert_shipment(db_path, scenario.shipment)
        for comm in scenario.communications:
            insert_communication(db_path, comm)
        if scenario.refund:
            insert_refund(db_path, scenario.refund)
        for doc in scenario.documents:
            insert_document(db_path, doc)

    strong_case = next(s for s in get_scenarios() if s.order.merchant_order_id == "ORD-1001")
    evidence = get_case_evidence(db_path, payment_id=strong_case.order.payment_id)
    assert evidence.order.product == "Smartphone (128GB)"
    assert evidence.shipment.delivery_status == "delivered"


def test_seed_scenarios_include_grounded_indian_dispute_patterns():
    """UPI payment confusion and RTO (return-to-origin) are the two dispute
    patterns most specific to how Indian merchants and Indian logistics
    actually operate - distinct from generic "item damaged" style disputes."""
    from src.merchant_seed_data import get_scenarios

    scenarios = get_scenarios()

    upi_scenarios = [s for s in scenarios if s.dispute_payment_method == "upi"]
    assert upi_scenarios, "expected at least one UPI-method dispute scenario"

    rto_scenarios = [
        s for s in scenarios
        if s.shipment is not None and s.shipment.delivery_status == "returned_to_sender"
    ]
    assert rto_scenarios, "expected at least one RTO (returned_to_sender) scenario"
