"""Seed the merchant database with synthetic chargeback investigation cases.

By default this also ingests a matching Simulated Test Dispute for each
scenario via the SAME Phase 2 entrypoint (src/ingestion.py) a real webhook
uses, linked to the scenario's own payment_id - so after running this script
there is a complete, ready-to-investigate case sitting in both databases:
merchant.db (what the shop's systems show) and cases.db (the dispute Razorpay
would have reported). That link (merchant_order_id -> razorpay_order_id ->
payment_id -> dispute_id) is exactly what the spec asks Phase 3 to establish.

Each case is then investigated immediately, by default: a human reviewer
should never have to manually trigger AI analysis before they can review and
decide - that is busywork, not human judgment. A fresh demo load therefore
lands with every case already showing an AI recommendation, ready to approve
or reject. Use --no-investigate to skip this (faster iteration while
developing seed data; the dashboard will still auto-investigate on open).

Usage:
    uv run scripts/seed_merchant_db.py                  # data + cases + AI investigation
    uv run scripts/seed_merchant_db.py --no-investigate  # skip the AI step
    uv run scripts/seed_merchant_db.py --merchant-only   # skip case ingestion entirely
    uv run scripts/seed_merchant_db.py --reset           # wipe both DBs first
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.database import init_case_db, save_investigation  # noqa: E402
from src.dispute_simulator import SimulatedDisputeSpec, build_simulated_case  # noqa: E402
from src.ingestion import ingest_dispute  # noqa: E402
from src.investigation_agent import investigate_dispute  # noqa: E402
from src.merchant_db import (  # noqa: E402
    init_merchant_db,
    insert_communication,
    insert_document,
    insert_order,
    insert_policy,
    insert_refund,
    insert_shipment,
)
from src.merchant_seed_data import get_policies, get_scenarios  # noqa: E402
from src.review_workflow import advance_to_review  # noqa: E402

DAY = 24 * 3600


def write_placeholder_document(documents_dir: Path, filename: str, document_type: str, description: str) -> None:
    """Write a small text stand-in for what would be a scanned document/photo
    in a real system, so later evidence-existence checks (spec section 9)
    have a real file to check for, not just a DB row. Clearly labelled as
    synthetic in its own content."""
    documents_dir.mkdir(parents=True, exist_ok=True)
    path = documents_dir / filename
    path.write_text(
        f"[SYNTHETIC DEMO DOCUMENT - NOT A REAL RECORD]\n"
        f"document_type: {document_type}\n\n"
        f"{description}\n"
    )


def main() -> int:
    reset = "--reset" in sys.argv
    with_cases = "--merchant-only" not in sys.argv
    investigate = with_cases and "--no-investigate" not in sys.argv

    settings = load_settings(require_razorpay=False)
    merchant_db = settings.paths.merchant_db
    case_db = settings.paths.case_db
    documents_dir = merchant_db.parent / "documents"

    if reset:
        merchant_db.unlink(missing_ok=True)
        if with_cases:
            case_db.unlink(missing_ok=True)
        print(f"[reset] removed existing DB file(s)")

    init_merchant_db(merchant_db)
    if with_cases:
        init_case_db(case_db)

    for policy in get_policies():
        insert_policy(merchant_db, policy)
    print(f"[merchant_db] {len(get_policies())} policy documents seeded")

    scenarios = get_scenarios()
    for i, scenario in enumerate(scenarios):
        insert_order(merchant_db, scenario.order)
        if scenario.shipment:
            insert_shipment(merchant_db, scenario.shipment)
        for comm in scenario.communications:
            insert_communication(merchant_db, comm)
        if scenario.refund:
            insert_refund(merchant_db, scenario.refund)
        for doc in scenario.documents:
            insert_document(merchant_db, doc)
            write_placeholder_document(documents_dir, doc.filename, doc.document_type, doc.description)

        case_state = ""
        if with_cases:
            respond_by_hours = 12 + i * 18  # stagger urgency across the demo set
            spec = SimulatedDisputeSpec(
                amount=scenario.order.amount, reason_code=scenario.dispute_reason_code,
                reason_description=scenario.dispute_reason_description,
                respond_by_hours_from_now=respond_by_hours,
                payment_method=scenario.dispute_payment_method,
                linked_real_payment_id=scenario.order.payment_id,
                linked_real_order_id=scenario.order.razorpay_order_id,
            )
            dispute, payment = build_simulated_case(spec)
            record = ingest_dispute(case_db, dispute, payment, source="simulated")
            case_state = f"  dispute={record.dispute_id} case_state={record.case_state}"

            if investigate:
                result = investigate_dispute(record.dispute_id, case_db, merchant_db, settings)
                save_investigation(case_db, result)
                if result.succeeded:
                    advance_to_review(case_db, record, actor="ai_investigation")
                    case_state = (
                        f"  dispute={record.dispute_id}  AI={result.classification} "
                        f"({result.confidence:.0%})  state=PENDING_HUMAN_REVIEW"
                    )
                else:
                    case_state = (
                        f"  dispute={record.dispute_id}  AI investigation FAILED: "
                        f"{result.failure_reason}"
                    )

        print(f"[seeded] {scenario.order.merchant_order_id:<10} "
              f"({scenario.expected_strength:<12}) {scenario.order.product}{case_state}")

    print(f"\nmerchant.db: {merchant_db}")
    if with_cases:
        print(f"cases.db:    {case_db}")
    print(f"documents:   {documents_dir}")
    mode = "merchant data only" if not with_cases else (
        "merchant + case data + AI investigation" if investigate else "merchant + case data"
    )
    print(f"\n{len(scenarios)} synthetic scenarios seeded ({mode}).")
    if investigate:
        print("Every case already has an AI recommendation - open the dashboard and review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
