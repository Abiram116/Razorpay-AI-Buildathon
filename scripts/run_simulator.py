"""Seed a Simulated Test Dispute directly into the case DB (no HTTP).

This is the same path the spec's example scenarios describe (STRONG_CASE /
NO_CASE) - it bypasses the webhook because Razorpay cannot originate a test
dispute for me (Phase 1 finding). It calls the identical ingest_dispute()
entrypoint a verified webhook uses, with source="simulated" explicit.

Usage:
    uv run scripts/run_simulator.py strong   # delivered + confirmed receipt
    uv run scripts/run_simulator.py no-case  # never shipped
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.database import init_case_db  # noqa: E402
from src.dispute_simulator import SimulatedDisputeSpec, build_simulated_case  # noqa: E402
from src.ingestion import ingest_dispute  # noqa: E402

SCENARIOS = {
    "strong": SimulatedDisputeSpec(
        amount=2_500_000, reason_code="goods_services_not_provided",
        reason_description="Product not received",
    ),
    "no-case": SimulatedDisputeSpec(
        amount=2_500_000, reason_code="goods_services_not_provided",
        reason_description="Product not received",
    ),
}


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "strong"
    if label not in SCENARIOS:
        print(f"unknown scenario {label!r}. choices: {list(SCENARIOS)}")
        return 1

    settings = load_settings(require_razorpay=False)
    init_case_db(settings.paths.case_db)

    dispute, payment = build_simulated_case(SCENARIOS[label])
    record = ingest_dispute(settings.paths.case_db, dispute, payment, source="simulated")

    print(f"[SIMULATED TEST DISPUTE] scenario={label!r}")
    print(f"  dispute_id : {record.dispute_id}  (sim_ prefix -> never a real Razorpay id)")
    print(f"  payment_id : {record.payment_id}")
    print(f"  amount     : {record.amount / 100:.2f} {record.currency}")
    print(f"  reason     : {record.reason_code}")
    print(f"  case_state : {record.case_state}")
    print(f"  is_simulated: {record.is_simulated}")
    print(f"\nStored in: {settings.paths.case_db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
