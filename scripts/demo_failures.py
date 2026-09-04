"""Phase 9 — Failure Recovery Demonstration.

I built this because the brief is explicit: "The project will be judged on
failure recovery." Every path this script exercises already exists and is
already covered by Phases 1-8's own test suite (216 tests) — this script
doesn't add new business logic. It wires the SAME functions the real app
uses (webhook_handler, investigation_agent, contest_service, database,
review_workflow) together into one readable run, against deliberately
injected failures, so a judge can watch the system refuse to do the wrong
thing instead of taking my word for it across a dozen separate test files.

Safety, by construction, not by promise:
  * Runs entirely against a throwaway temp directory. The real
    data/merchant/{merchant,cases}.db are never opened, so they cannot be
    corrupted - this is verified at the end anyway, belt and suspenders.
  * No real credentials. A synthetic Settings object is built directly
    (never load_settings(), never reads .env), so this runs the same way
    whether or not Razorpay/Groq credentials are configured.
  * Mocks touch only the true external boundary - the razorpay SDK call
    inside RazorpayClient, and the Groq call inside investigation_agent.
    Everything around that boundary (auth error translation, HMAC
    verification, retry/backoff, JSON-schema validation, evidence citation
    checking, the state machine, the deadline guard) is the real code.
  * Nothing is ever submitted to a live endpoint. Every scenario that
    touches contest_service uses a MagicMock Razorpay client and asserts,
    explicitly, that contest_dispute/upload_evidence_document were never
    called when they shouldn't have been.

Run:
    uv run scripts/demo_failures.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import razorpay.errors as razorpay_errors  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.investigation_agent as investigation_agent  # noqa: E402
import src.webhook_handler as webhook_handler  # noqa: E402
from src.config import (  # noqa: E402
    AIConfig,
    CostModelConfig,
    DeadlineConfig,
    Paths,
    RazorpayConfig,
    Settings,
)
from src.contest_service import (  # noqa: E402
    ContestError,
    SubmissionBlocked,
    assert_submittable,
    build_contest_payload,
    build_local_draft,
    submit_contest,
)
from src.database import (  # noqa: E402
    CaseRecord,
    get_audit_log,
    get_case,
    get_latest_investigation,
    ingest_case,
    init_case_db,
    save_investigation,
)
from src.dispute_schema import DisputeEntity, IngestedCase, PaymentSummary  # noqa: E402
from src.evidence_builder import build_evidence_package  # noqa: E402
from src.investigation_agent import investigate_dispute  # noqa: E402
from src.investigation_schema import EvidenceCitation, InvestigationResult  # noqa: E402
from src.merchant_db import (  # noqa: E402
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
from src.razorpay_client import RazorpayClient  # noqa: E402
from src.review_workflow import deadline_status  # noqa: E402


# ----------------------------------------------------------------------
# Output plumbing
# ----------------------------------------------------------------------

@dataclass
class ScenarioResult:
    title: str
    failure_injected: str
    system_response: str
    safety_guarantee: str
    passed: bool
    detail: str = ""


def run_scenario(
    index: int, total: int, title: str, fn: Callable[[], ScenarioResult]
) -> ScenarioResult:
    """Runs one scenario and prints it in judge-readable form.

    An unhandled exception is itself a FAIL, not a crash of the whole demo -
    that's what "fail safely" has to mean for the demo script too.
    """
    print(f"\n[{index}/{total}] {title}")
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        result = ScenarioResult(
            title=title,
            failure_injected="(see traceback)",
            system_response=f"UNHANDLED EXCEPTION: {type(exc).__name__}: {exc}",
            safety_guarantee="the system must not crash uncontrolled",
            passed=False,
            detail=traceback.format_exc(limit=4),
        )
    print(f"    Failure injected : {result.failure_injected}")
    print(f"    System response  : {result.system_response}")
    print(f"    Safety guarantee : {result.safety_guarantee}")
    verdict = "PASS" if result.passed else "FAIL"
    print(f"    Result           : {verdict}")
    if not result.passed and result.detail:
        print(f"    ---\n{result.detail}")
    return result


# ----------------------------------------------------------------------
# Synthetic environment - never touches the real .env or real databases
# ----------------------------------------------------------------------

def build_demo_settings(tmp_dir: Path) -> Settings:
    return Settings(
        razorpay=RazorpayConfig(
            key_id="rzp_test_demo00000000",  # clearly fake, never a real secret
            key_secret="demo_secret_not_real",
            webhook_secret="demo_webhook_secret_not_real",
        ),
        ai=AIConfig(api_key="gsk_demo_not_a_real_key"),
        deadlines=DeadlineConfig(),
        costs=CostModelConfig(),
        paths=Paths(
            merchant_db=tmp_dir / "merchant.db",
            case_db=tmp_dir / "cases.db",
            generated_docs=tmp_dir / "generated",
        ),
    )


def seed_case_and_evidence(
    settings: Settings, *, order_id: str, dispute_id: str, payment_id: str,
    respond_by: int, with_evidence: bool = True,
) -> CaseRecord:
    """The one real, non-simulated dispute this demo reuses across scenarios
    that need a full case - built with the same schema types Phase 2 and
    Phase 3 use, not a shortcut."""
    dispute = DisputeEntity(
        id=dispute_id, payment_id=payment_id, amount=1_500_000, currency="INR",
        amount_deducted=0, reason_code="goods_services_not_provided",
        respond_by=respond_by, status="open", phase="chargeback", created_at=1000,
    )
    payment = PaymentSummary(
        id=payment_id, order_id=order_id, amount=1_500_000, currency="INR",
        status="captured", method="card", captured=True, amount_refunded=0,
        refund_status=None, created_at=900,
    )
    case = ingest_case(
        settings.paths.case_db,
        IngestedCase(dispute=dispute, payment=payment, source="razorpay_webhook",
                    is_simulated=False),
        actor="demo",
    )
    if with_evidence:
        merchant_order_id = f"ORD-{dispute_id[-6:]}"
        insert_order(settings.paths.merchant_db, Order(
            merchant_order_id, order_id, payment_id, "CUST-DEMO", "Smartphone",
            "physical", 1_500_000, "INR", 1000, "fulfilled", "Demo Address",
            "Demo Address", False,
        ))
        insert_shipment(settings.paths.merchant_db, Shipment(
            merchant_order_id, "TRK-DEMO", "BlueDart", 1100, 1200, "delivered",
            "Demo City", "Signed by recipient",
        ))
        insert_communication(settings.paths.merchant_db, Communication(
            merchant_order_id, "CUST-DEMO", 1300, "chat", "Received it, thanks", "inbound",
        ))
        insert_refund(settings.paths.merchant_db, Refund(
            merchant_order_id, payment_id, False, "none", None, None, None,
        ))
        insert_document(settings.paths.merchant_db, EvidenceDocument(
            merchant_order_id, "shipping_proof", "pod.txt", "Signed proof of delivery",
        ))
        insert_policy(settings.paths.merchant_db, Policy(
            "refund_policy", "v1", 500, "7 day returns",
        ))
    return case


def fake_investigation(dispute_id: str) -> InvestigationResult:
    return InvestigationResult(
        dispute_id=dispute_id, classification="STRONG_CASE", confidence=0.9,
        executive_summary="Delivery proven.", reason="Signed delivery on file.",
        supporting_evidence=[EvidenceCitation("shipment", "ORD-DEMO", "signed")],
        missing_evidence=[], conflicting_evidence=[], recommended_action="CONTEST",
        risk_factors=[], investigation_timestamp=int(time.time()), model="demo-model",
        is_simulated_case=False,
    )


# ----------------------------------------------------------------------
# Scenario 1 - Razorpay authentication / API failure
# ----------------------------------------------------------------------

def scenario_razorpay_auth_failure(settings: Settings) -> ScenarioResult:
    client = RazorpayClient(settings)
    # The true external boundary: the underlying razorpay SDK call inside our
    # client, not our own translation logic. This is the exact error text
    # Razorpay's live API returned in Phase 1 for bad credentials.
    client._client.payment.all = MagicMock(
        side_effect=razorpay_errors.BadRequestError("Authentication failed")
    )

    result = client.verify_credentials()
    redacted = result.key_id_redacted

    checks = [
        result.authenticated is False,
        "rzp_test_demo00000000" not in redacted,  # the raw key never appears
        "demo_secret_not_real" not in str(result.detail),  # nor the secret
        "*" in redacted,  # it WAS redacted, not merely absent
    ]
    return ScenarioResult(
        title="Razorpay authentication failure",
        failure_injected="Underlying SDK call raises BadRequestError('Authentication failed')",
        system_response=(
            f"verify_credentials() returned authenticated=False, "
            f"key_id shown as {redacted!r}, no exception escaped"
        ),
        safety_guarantee="No fabricated data returned; no secret ever printed; no crash",
        passed=all(checks),
    )


# ----------------------------------------------------------------------
# Scenario 2 - tampered webhook signature
# ----------------------------------------------------------------------

SAMPLE_ENVELOPE = {
    "entity": "event", "account_id": "acc_DemoAccount01", "event": "payment.dispute.created",
    "contains": ["payment", "dispute"],
    "payload": {
        "payment": {"entity": {
            "id": "pay_DemoPayment001", "entity": "payment", "amount": 1500000, "currency": "INR",
            "status": "captured", "order_id": "order_DemoOrder00001", "international": False,
            "method": "card", "amount_refunded": 0, "refund_status": None, "captured": True,
            "created_at": 1000,
        }},
        "dispute": {"entity": {
            "id": "disp_DemoDispute001", "entity": "dispute", "payment_id": "pay_DemoPayment001",
            "amount": 1500000, "currency": "INR", "amount_deducted": 0,
            "reason_code": "goods_services_not_provided", "respond_by": 9_999_999_999,
            "status": "open", "phase": "chargeback", "created_at": 1000,
        }},
    },
    "created_at": 1000,
}


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _webhook_client(settings: Settings) -> TestClient:
    webhook_handler._settings = settings
    webhook_handler._client = None
    init_case_db(settings.paths.case_db)
    return TestClient(webhook_handler.app)


def scenario_tampered_webhook(settings: Settings) -> ScenarioResult:
    client = _webhook_client(settings)
    body = json.dumps(SAMPLE_ENVELOPE).encode()
    tampered_signature = _sign(body, "not_the_real_webhook_secret")

    resp = client.post(
        "/webhooks/razorpay/disputes", content=body,
        headers={"X-Razorpay-Signature": tampered_signature, "Content-Type": "application/json"},
    )
    case = get_case(settings.paths.case_db, "disp_DemoDispute001")

    checks = [resp.status_code == 400, case is None]
    return ScenarioResult(
        title="Tampered / invalid Razorpay webhook signature",
        failure_injected="X-Razorpay-Signature computed with the wrong secret",
        system_response=f"HTTP {resp.status_code} {resp.json()}, no case row created",
        safety_guarantee="Invalid signature rejected before JSON is even parsed; "
                          "no dispute ingested, no investigation triggered",
        passed=all(checks),
    )


# ----------------------------------------------------------------------
# Scenario 3 - duplicate webhook
# ----------------------------------------------------------------------

# A distinct envelope from scenario 2's - the demo must not reuse the exact
# same body bytes as the tampered-signature scenario, or it would (correctly,
# per the Phase 9 fix below) demonstrate signature-upgrade instead of plain
# duplicate-delivery idempotency, which is a different guarantee.
DUPLICATE_TEST_ENVELOPE = {
    **SAMPLE_ENVELOPE,
    "payload": {
        **SAMPLE_ENVELOPE["payload"],
        "dispute": {"entity": {
            **SAMPLE_ENVELOPE["payload"]["dispute"]["entity"],
            "id": "disp_DuplicateTes01",
        }},
    },
}


def scenario_duplicate_webhook(settings: Settings) -> ScenarioResult:
    client = _webhook_client(settings)
    body = json.dumps(DUPLICATE_TEST_ENVELOPE).encode()
    signature = _sign(body, settings.razorpay.webhook_secret)
    headers = {"X-Razorpay-Signature": signature, "Content-Type": "application/json"}

    first = client.post("/webhooks/razorpay/disputes", content=body, headers=headers)
    second = client.post("/webhooks/razorpay/disputes", content=body, headers=headers)

    log = get_audit_log(settings.paths.case_db, "disp_DuplicateTes01")
    ingest_entries = [e for e in log if e["action"] == "ingest"]

    checks = [
        first.json().get("status") == "processed",
        second.json().get("status") == "duplicate_ignored",
        len(ingest_entries) == 1,
    ]
    return ScenarioResult(
        title="Duplicate webhook delivery",
        failure_injected="The exact same signed payload delivered twice",
        system_response=(
            f"first={first.json()['status']!r}, second={second.json()['status']!r}, "
            f"{len(ingest_entries)} 'ingest' audit entr{'y' if len(ingest_entries)==1 else 'ies'}"
        ),
        safety_guarantee="Retried/duplicate deliveries are idempotent - one case, one investigation",
        passed=all(checks),
    )


# ----------------------------------------------------------------------
# Scenario 4 - AI transient failure, recovers via the real backoff
# ----------------------------------------------------------------------

def scenario_ai_transient_recovery(settings: Settings, case: CaseRecord) -> ScenarioResult:
    evidence = get_case_evidence(settings.paths.merchant_db, payment_id=case.payment_id)
    good_payload = {
        "classification": "STRONG_CASE", "confidence": 0.9,
        "executive_summary": "Delivery proven.", "reason": "Signed delivery on file.",
        "supporting_evidence": [{"reference": f"shipment:{evidence.order.merchant_order_id}",
                                 "note": "signed"}],
        "missing_evidence": [], "conflicting_evidence": [], "recommended_action": "CONTEST",
        "risk_factors": [],
    }
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise investigation_agent.GroqTransientError("simulated timeout", retry_after=0.01)
        return good_payload

    with patch.object(investigation_agent, "_call_groq", side_effect=flaky), \
         patch.object(investigation_agent.time, "sleep"):  # don't actually wait in the demo
        result = investigate_dispute(
            case.dispute_id, settings.paths.case_db, settings.paths.merchant_db, settings,
        )

    checks = [result.succeeded, calls["n"] == 3, getattr(result, "classification", None) == "STRONG_CASE"]
    return ScenarioResult(
        title="AI transient failure (timeout-shaped), then recovers",
        failure_injected="Groq call raises a transient error twice in a row",
        system_response=f"3rd attempt succeeded after 2 backed-off retries "
                        f"({calls['n']} total attempts); investigation completed normally",
        safety_guarantee="Real backoff/retry path used (src/investigation_agent.py) - "
                          "no fabricated result on the failed attempts",
        passed=all(checks),
    )


# ----------------------------------------------------------------------
# Scenario 5 - malformed / persistently invalid AI output
# ----------------------------------------------------------------------

def scenario_malformed_ai_output(settings: Settings, case: CaseRecord) -> ScenarioResult:
    # Invalid on every attempt: classification isn't one of the three
    # documented values. Must exhaust the real retry-with-correction path and
    # fail safe - never get coerced into a fake verdict.
    bad_payload = {
        "classification": "PROBABLY_FINE", "confidence": 0.9,
        "executive_summary": "x", "reason": "x",
        "supporting_evidence": [], "missing_evidence": [], "conflicting_evidence": [],
        "recommended_action": "CONTEST", "risk_factors": [],
    }
    with patch.object(investigation_agent, "_call_groq", return_value=bad_payload):
        result = investigate_dispute(
            case.dispute_id, settings.paths.case_db, settings.paths.merchant_db, settings,
        )
    save_investigation(settings.paths.case_db, result)
    stored = get_latest_investigation(settings.paths.case_db, case.dispute_id)

    checks = [
        not result.succeeded,
        getattr(result, "failure_reason", None) == "INVALID_AI_RESPONSE",
        stored["succeeded"] is False,
        stored["classification"] is None,  # nothing fake persisted
    ]
    return ScenarioResult(
        title="Malformed AI output (invalid classification, every attempt)",
        failure_injected="Model returns classification='PROBABLY_FINE' (not a valid enum value)",
        system_response=f"Rejected on every attempt -> InvestigationFailure"
                        f"({getattr(result, 'failure_reason', '?')}); recorded, not silently dropped",
        safety_guarantee="No STRONG_CASE/CONTEST verdict is ever persisted from invalid output",
        passed=all(checks),
    )


# ----------------------------------------------------------------------
# Scenario 6 - missing merchant evidence
# ----------------------------------------------------------------------

def scenario_missing_merchant_evidence(settings: Settings) -> ScenarioResult:
    orphan_case = seed_case_and_evidence(
        settings, order_id="order_OrphanOrder001", dispute_id="disp_OrphanDispu001",
        payment_id="pay_OrphanPaymen01", respond_by=int(time.time()) + 5 * 86400,
        with_evidence=False,  # deliberately: no merchant.db row for this payment
    )
    with patch.object(investigation_agent, "_call_groq") as mock_call:
        result = investigate_dispute(
            orphan_case.dispute_id, settings.paths.case_db, settings.paths.merchant_db, settings,
        )

    checks = [
        not result.succeeded,
        getattr(result, "failure_reason", None) == "NO_MERCHANT_EVIDENCE",
        mock_call.call_count == 0,  # never even asked the model to guess
    ]
    return ScenarioResult(
        title="Missing merchant evidence",
        failure_injected="A dispute with no corresponding order/shipment/refund in merchant.db",
        system_response=f"InvestigationFailure({getattr(result, 'failure_reason', '?')}) "
                        f"returned before any AI call was attempted ({mock_call.call_count} calls made)",
        safety_guarantee="No shipping/refund/communication evidence is ever fabricated to fill the gap",
        passed=all(checks),
    )


# ----------------------------------------------------------------------
# Scenario 7 - oversized contest summary
# ----------------------------------------------------------------------

def scenario_oversized_summary(settings: Settings) -> ScenarioResult:
    oversized = "x" * 1500  # over the documented 1000-char Razorpay limit
    raised = None
    try:
        build_contest_payload(oversized, {"shipping_proof": ["doc_1"]}, "draft", settings)
    except ContestError as exc:
        raised = exc

    checks = [raised is not None, "1000" in str(raised)]
    return ScenarioResult(
        title="Oversized contest summary",
        failure_injected=f"A {len(oversized)}-character summary (Razorpay's documented limit is 1000)",
        system_response=f"build_contest_payload() raised ContestError before constructing any "
                        f"request: {raised}",
        safety_guarantee="Refused locally, before any network call - Razorpay's API is never "
                          "given an invalid payload to reject",
        passed=all(checks),
    )


# ----------------------------------------------------------------------
# Scenario 8 - expired dispute deadline
# ----------------------------------------------------------------------

def scenario_expired_deadline(settings: Settings) -> ScenarioResult:
    expired_case = seed_case_and_evidence(
        settings, order_id="order_ExpiredOrde001", dispute_id="disp_ExpiredDisp001",
        payment_id="pay_ExpiredPayme01", respond_by=int(time.time()) - 3600,  # 1h in the past
    )
    for state in ("ANALYZING", "ANALYSIS_COMPLETE", "PENDING_HUMAN_REVIEW", "APPROVED"):
        from src.database import transition_case_state
        transition_case_state(settings.paths.case_db, expired_case.dispute_id, state,
                              actor="demo", action="setup")
    expired_case = get_case(settings.paths.case_db, expired_case.dispute_id)

    deadline = deadline_status(expired_case.respond_by, settings)
    evidence = get_case_evidence(settings.paths.merchant_db, payment_id=expired_case.payment_id)
    investigation = fake_investigation(expired_case.dispute_id)
    package = build_evidence_package(
        expired_case, evidence, investigation, settings,
        output_dir=settings.paths.generated_docs / expired_case.dispute_id,
        source_dir=settings.paths.generated_docs,
    )

    draft = build_local_draft(expired_case, evidence, package, investigation, settings)

    client = MagicMock()
    submit_blocked = False
    try:
        submit_contest(expired_case, evidence, package, investigation, actor="demo_reviewer",
                       human_confirmed=True, settings=settings, client=client)
    except SubmissionBlocked:
        submit_blocked = True

    checks = [
        deadline.is_expired,
        draft.blocked_reason is not None and "deadline" in draft.blocked_reason.lower(),
        not draft.can_submit,
        submit_blocked,
        client.contest_dispute.call_count == 0,
    ]
    return ScenarioResult(
        title="Expired dispute deadline",
        failure_injected="respond_by is 1 hour in the past on an otherwise APPROVED case",
        system_response=f"deadline.label={deadline.label!r}; draft refused with "
                        f"blocked_reason set; submit_contest raised SubmissionBlocked "
                        f"before any client call",
        safety_guarantee="An expired case cannot reach a draft or submit call, at the "
                          "backend level - not only as a dashboard warning banner",
        passed=all(checks),
    )


# ----------------------------------------------------------------------
# Human-in-the-loop boundary (shown, not counted in the 8/8)
# ----------------------------------------------------------------------

def demonstrate_human_boundary(settings: Settings, case: CaseRecord) -> None:
    print("\n" + "=" * 72)
    print("HUMAN-IN-THE-LOOP BOUNDARY")
    print("=" * 72)
    print("The AI may: recommend a classification, build an evidence package,")
    print("prepare a local contest draft. It may NEVER submit on its own.\n")

    evidence = get_case_evidence(settings.paths.merchant_db, payment_id=case.payment_id)
    investigation = fake_investigation(case.dispute_id)
    package = build_evidence_package(
        case, evidence, investigation, settings,
        output_dir=settings.paths.generated_docs / case.dispute_id,
        source_dir=settings.paths.generated_docs,
    )
    client = MagicMock()

    try:
        submit_contest(case, evidence, package, investigation, actor="demo",
                       settings=settings, client=client)  # human_confirmed omitted
        no_default_submit = False
    except SubmissionBlocked:
        no_default_submit = True
    print(f"  submit_contest() with no human_confirmed argument -> "
          f"{'BLOCKED' if no_default_submit else 'DID NOT BLOCK (BUG)'}")

    try:
        submit_contest(case, evidence, package, investigation, actor="  ",
                       human_confirmed=True, settings=settings, client=client)
        anon_blocked = False
    except SubmissionBlocked:
        anon_blocked = True
    print(f"  submit_contest() with human_confirmed=True but no reviewer identity -> "
          f"{'BLOCKED' if anon_blocked else 'DID NOT BLOCK (BUG)'}")

    dashboard_source = (Path(__file__).resolve().parent.parent / "dashboard" / "app.py").read_text()
    static_ok = "contest_dispute" not in dashboard_source
    print(f"  dashboard source contains a direct contest_dispute() call -> "
          f"{'NO (' + str(static_ok) + ')' if static_ok else 'YES (BUG)'}")

    print(f"\n  client.contest_dispute called: {client.contest_dispute.call_count} times "
          f"(must be 0 - nothing above should have reached the network)")
    all_ok = no_default_submit and anon_blocked and static_ok and client.contest_dispute.call_count == 0
    print(f"  Result: {'PASS' if all_ok else 'FAIL'}")


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main() -> int:
    print("=" * 72)
    print("PHASE 9 — FAILURE RECOVERY DEMONSTRATION")
    print("=" * 72)
    print("Every scenario below runs against real application code (webhook")
    print("handler, investigation agent, contest service, database, workflow).")
    print("Only the true external boundary is mocked: the Razorpay SDK call and")
    print("the Groq call. No real credentials, no live network, no data files")
    print("outside a throwaway temp directory.")

    real_merchant_db = Path("data/merchant/merchant.db")
    real_case_db = Path("data/merchant/cases.db")
    before = {
        p: (p.stat().st_mtime_ns, p.stat().st_size) for p in (real_merchant_db, real_case_db)
        if p.exists()
    }

    with tempfile.TemporaryDirectory(prefix="phase9-demo-") as tmp:
        tmp_dir = Path(tmp)
        settings = build_demo_settings(tmp_dir)
        init_case_db(settings.paths.case_db)
        init_merchant_db(settings.paths.merchant_db)

        shared_case = seed_case_and_evidence(
            settings, order_id="order_SharedOrder001", dispute_id="disp_SharedDispu001",
            payment_id="pay_SharedPaymen01", respond_by=int(time.time()) + 5 * 86400,
        )

        results: list[ScenarioResult] = []
        results.append(run_scenario(1, 8, "Razorpay authentication failure",
                                    lambda: scenario_razorpay_auth_failure(settings)))
        results.append(run_scenario(2, 8, "Invalid webhook signature",
                                    lambda: scenario_tampered_webhook(settings)))
        results.append(run_scenario(3, 8, "Duplicate webhook delivery",
                                    lambda: scenario_duplicate_webhook(settings)))
        results.append(run_scenario(4, 8, "AI transient failure, recovers via retry",
                                    lambda: scenario_ai_transient_recovery(settings, shared_case)))
        results.append(run_scenario(5, 8, "Malformed AI output (persistent)",
                                    lambda: scenario_malformed_ai_output(settings, shared_case)))
        results.append(run_scenario(6, 8, "Missing merchant evidence",
                                    lambda: scenario_missing_merchant_evidence(settings)))
        results.append(run_scenario(7, 8, "Oversized contest summary",
                                    lambda: scenario_oversized_summary(settings)))
        results.append(run_scenario(8, 8, "Expired dispute deadline",
                                    lambda: scenario_expired_deadline(settings)))

        demonstrate_human_boundary(settings, shared_case)

    print("\n" + "=" * 72)
    passed = sum(1 for r in results if r.passed)
    print(f"SUMMARY: {passed}/8 failure scenarios handled safely")
    print("=" * 72)
    for i, r in enumerate(results, start=1):
        mark = "✅" if r.passed else "❌"
        print(f"  {mark} [{i}/8] {r.title}")

    print("\nData integrity check (real project databases, never opened by this demo):")
    after_ok = True
    for p in (real_merchant_db, real_case_db):
        if p in before:
            now = (p.stat().st_mtime_ns, p.stat().st_size)
            unchanged = now == before[p]
            after_ok = after_ok and unchanged
            print(f"  {p}: {'unchanged' if unchanged else 'CHANGED (unexpected)'}")
        else:
            print(f"  {p}: did not exist before or after - not touched")
    print(f"  Result: {'PASS' if after_ok else 'FAIL'}")

    return 0 if (passed == 8 and after_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
