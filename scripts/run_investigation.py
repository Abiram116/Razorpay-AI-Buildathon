"""Investigate seeded disputes with the AI agent (Phase 4).

Usage:
    uv run scripts/run_investigation.py                 # all open cases
    uv run scripts/run_investigation.py <dispute_id>    # one case
    uv run scripts/run_investigation.py --verbose       # full reasoning

Results are persisted to the investigations table. This NEVER submits or
contests anything - it produces a recommendation for a human (Phase 6/7).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.database import list_cases, save_investigation  # noqa: E402
from src.investigation_agent import investigate_dispute  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    verbose = "--verbose" in sys.argv
    settings = load_settings(require_razorpay=False)

    cases = list_cases(settings.paths.case_db)
    if args:
        cases = [c for c in cases if c.dispute_id == args[0]]
        if not cases:
            print(f"No case with dispute_id={args[0]!r}. Seed first: "
                  "uv run scripts/seed_merchant_db.py --reset")
            return 1
    if not cases:
        print("No cases to investigate. Run: uv run scripts/seed_merchant_db.py --reset")
        return 1

    failures = 0
    for case in cases:
        label = "SIMULATED" if case.is_simulated else "RAZORPAY"
        result = investigate_dispute(
            case.dispute_id, settings.paths.case_db, settings.paths.merchant_db, settings
        )
        save_investigation(settings.paths.case_db, result)

        print(f"\n{'=' * 72}")
        print(f"[{label}] {case.dispute_id}  {case.currency} {case.amount / 100:,.2f}  "
              f"reason={case.reason_code}")
        print("=" * 72)
        if not result.succeeded:
            failures += 1
            print(f"  INVESTIGATION FAILED ({result.failure_reason})")
            print(f"  {result.detail}")
            continue

        print(f"  AI RECOMMENDATION : {result.classification}  "
              f"(confidence {result.confidence:.0%})")
        print(f"  SUGGESTED ACTION  : {result.recommended_action}  [human decides]")
        print(f"  SUMMARY           : {result.executive_summary}")
        if verbose:
            print(f"  REASON            : {result.reason}")
        print("  EVIDENCE CITED    :")
        for c in result.supporting_evidence:
            print(f"      - {c.reference}: {c.note}")
        if result.missing_evidence:
            print("  MISSING EVIDENCE  :")
            for m in result.missing_evidence:
                print(f"      - {m}")
        if result.conflicting_evidence:
            print("  CONFLICTS FOUND   :")
            for c in result.conflicting_evidence:
                print(f"      - {c}")
        if result.risk_factors:
            print(f"  RISK FACTORS      : {', '.join(result.risk_factors)}")

    print(f"\n{len(cases) - failures}/{len(cases)} investigated successfully.")
    print("No dispute has been contested or submitted - human approval required (Phase 6/7).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
