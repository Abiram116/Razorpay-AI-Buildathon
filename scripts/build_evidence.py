"""Build evidence packages from completed investigations (Phase 5).

Usage:
    uv run scripts/build_evidence.py                  # all investigated cases
    uv run scripts/build_evidence.py <dispute_id>
    uv run scripts/build_evidence.py <dispute_id> --force   # override a NO_CASE

Produces, per case, under data/generated/<dispute_id>/:
  * one PDF per merchant evidence document (Razorpay accepts PDF/PNG/JPG only)
  * an explanation letter PDF
  * a chargeback defence report PDF for the human reviewer

This NEVER uploads to Razorpay and NEVER contests anything. Phase 7 does that,
behind explicit human approval.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.database import get_latest_investigation, list_cases  # noqa: E402
from src.document_generator import (  # noqa: E402
    generate_case_report_pdf,
    generate_explanation_letter_pdf,
)
from src.evidence_builder import EvidenceBuildError, build_evidence_package  # noqa: E402
from src.investigation_schema import InvestigationResult  # noqa: E402
from src.merchant_db import get_case_evidence  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    settings = load_settings(require_razorpay=False)

    cases = list_cases(settings.paths.case_db)
    if args:
        cases = [c for c in cases if c.dispute_id == args[0]]
        if not cases:
            print(f"No case with dispute_id={args[0]!r}")
            return 1

    built = skipped = 0
    for case in cases:
        stored = get_latest_investigation(settings.paths.case_db, case.dispute_id)
        if stored is None or not stored["succeeded"]:
            print(f"\n{case.dispute_id}: no successful investigation yet - run "
                  "scripts/run_investigation.py first")
            skipped += 1
            continue

        investigation = InvestigationResult.from_dict(stored["result"])
        evidence = get_case_evidence(settings.paths.merchant_db, payment_id=case.payment_id)
        if evidence is None:
            print(f"\n{case.dispute_id}: no merchant evidence on file - cannot build")
            skipped += 1
            continue

        label = "SIMULATED" if case.is_simulated else "RAZORPAY"
        print(f"\n{'=' * 72}")
        print(f"[{label}] {case.dispute_id}  {case.currency} {case.amount / 100:,.2f}  "
              f"AI: {investigation.classification}")
        print("=" * 72)

        try:
            package = build_evidence_package(
                case, evidence, investigation, settings, force=force
            )
        except EvidenceBuildError as exc:
            print(f"  NOT BUILT: {exc}")
            skipped += 1
            continue

        out_dir = settings.paths.generated_docs / case.dispute_id
        generate_explanation_letter_pdf(
            case, evidence, investigation, package.explanation_letter, out_dir
        )
        report = generate_case_report_pdf(
            case, evidence, investigation, out_dir,
            package.contest_summary, package.evidence_categories,
        )

        print("  EVIDENCE CATEGORIES (only those with real records behind them):")
        for category, refs in sorted(package.evidence_categories.items()):
            print(f"      {category:<28} <- {', '.join(refs)}")
        print(f"  CONTEST SUMMARY : {package.summary_trace.final_length}/"
              f"{package.summary_trace.limit} chars"
              + (" (AI-shortened)" if package.summary_trace.was_shortened_by_ai else "")
              + (" (TRUNCATED)" if package.summary_trace.was_truncated else ""))
        print(f"  DOCUMENTS       : {len(package.generated_documents)} evidence PDF(s)")
        print(f"  REVIEW REPORT   : {report.path.name}")
        print(f"  SUBMITTABLE     : {package.is_submittable} "
              f"(structurally - human approval still required)")
        for warning in package.warnings:
            print(f"  WARNING         : {warning}")
        built += 1

    print(f"\n{built} package(s) built, {skipped} skipped.")
    print("Nothing has been uploaded or contested - human approval required (Phase 6/7).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
