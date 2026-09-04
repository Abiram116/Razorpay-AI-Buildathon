"""Run the held-out evaluation (Phase 8).

Designed around one constraint: Groq's free tier gives ~8,000 tokens/minute,
and each case costs ~2.5k. That's roughly 3 cases a minute, so a 200-case run
takes over an hour no matter how it's written. Anything that takes an hour
gets interrupted, so this is built to be stopped and resumed at any point
without losing or re-doing work.

  * Every case is committed to the DB the instant it finishes.
  * A resumed run skips everything already stored - no wasted API budget.
  * Requests are paced BEFORE sending, so the rate limit is mostly never hit
    rather than hit-and-retried.
  * Ctrl-C finishes cleanly, records the run as interrupted, and tells you
    the command to resume.

Usage:
    uv run scripts/run_evaluation.py --split holdout          # the 50-case set
    uv run scripts/run_evaluation.py --split dev              # the 150-case set
    uv run scripts/run_evaluation.py --split all
    uv run scripts/run_evaluation.py --split holdout --limit 5   # smoke test
    uv run scripts/run_evaluation.py --split holdout --report    # metrics only
    uv run scripts/run_evaluation.py --list                   # past runs

Long runs are best backgrounded:
    nohup uv run scripts/run_evaluation.py --split all > eval.log 2>&1 &
"""

from __future__ import annotations

import argparse
import csv
import json
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.evaluation_dataset import (  # noqa: E402
    DATASET_VERSION,
    dataset_fingerprint,
    dataset_summary,
    generate_dataset,
)
from src.evaluation_metrics import compute_metrics  # noqa: E402
from src.evaluation_store import (  # noqa: E402
    completed_case_ids,
    get_run,
    init_evaluation_db,
    list_runs,
    load_metrics,
    load_results,
    make_run_id,
    mark_run,
    record_result,
    save_metrics,
    start_or_resume_run,
)
from src.investigation_agent import build_investigation_prompt, investigate  # noqa: E402
from src.rate_limiter import TokenBudgetPacer, estimate_tokens  # noqa: E402

_INTERRUPTED = False


def _handle_sigint(signum, frame):
    """First Ctrl-C asks for a clean stop; a second one is taken literally."""
    global _INTERRUPTED
    if _INTERRUPTED:
        print("\nSecond interrupt - exiting immediately.")
        sys.exit(130)
    _INTERRUPTED = True
    print("\nInterrupt received. Finishing the case in flight, then stopping "
          "cleanly (progress is already saved).")


def evaluation_db_path(settings) -> Path:
    return settings.paths.merchant_db.parent.parent / "evaluation" / "evaluation.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the chargeback evaluation set.")
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="holdout")
    parser.add_argument("--limit", type=int, default=None,
                        help="only run the first N pending cases (smoke test)")
    parser.add_argument("--report", action="store_true",
                        help="recompute and print metrics from stored results, run nothing")
    parser.add_argument("--list", action="store_true", help="list past runs and exit")
    parser.add_argument("--tpm", type=int, default=None,
                        help="override the tokens-per-minute budget")
    parser.add_argument("--json-out", type=str, default=None,
                        help="also write the metrics JSON to this path")
    args = parser.parse_args()

    settings = load_settings(require_razorpay=False)
    db_path = evaluation_db_path(settings)
    init_evaluation_db(db_path)

    if args.list:
        runs = list_runs(db_path)
        if not runs:
            print("No evaluation runs recorded yet.")
            return 0
        print(f"{'run_id':<40} {'split':<8} {'status':<12} {'model'}")
        for r in runs:
            print(f"{r['run_id']:<40} {r['split']:<8} {r['status']:<12} {r['model']}")
        return 0

    full_dataset = generate_dataset()
    fingerprint = dataset_fingerprint(full_dataset)
    dataset = full_dataset
    if args.split != "all":
        dataset = [c for c in dataset if c.split == args.split]

    run_id = make_run_id(DATASET_VERSION, args.split, settings.ai.model, fingerprint)

    if args.report:
        return _report(db_path, run_id, settings, args.json_out)

    signal.signal(signal.SIGINT, _handle_sigint)

    resumed = start_or_resume_run(
        db_path, run_id, DATASET_VERSION, args.split, settings.ai.model,
        len(dataset), {"tpm": args.tpm, "limit": args.limit},
    )
    done = completed_case_ids(db_path, run_id)
    pending = [c for c in dataset if c.case_id not in done]
    if args.limit:
        pending = pending[: args.limit]

    summary = dataset_summary(dataset)
    print("=" * 72)
    print(f"EVALUATION  split={args.split}  model={settings.ai.model}")
    print("=" * 72)
    print(f"run_id       : {run_id}")
    print(f"dataset hash : {fingerprint} (changes if the generator changes)")
    print(f"dataset      : {summary['total']} cases, "
          f"{summary['defensible_pct']}% defensible "
          f"({summary['by_ground_truth']})")
    if resumed:
        print(f"RESUMING     : {len(done)} already done, {len(pending)} to go")
    else:
        print(f"starting     : {len(pending)} cases")
    if not pending:
        print("\nNothing pending - computing metrics from stored results.")
        return _report(db_path, run_id, settings, args.json_out)

    pacer = TokenBudgetPacer(
        tokens_per_minute=args.tpm or TokenBudgetPacer.tokens_per_minute
    )
    print(f"pacing       : {pacer.budget} tokens/min "
          f"({int(pacer.tokens_per_minute)} limit x {pacer.safety_margin})")
    print(f"est. runtime : ~{len(pending) * 2500 / pacer.budget:.0f} min "
          "(rate-limit bound, not compute bound)")
    print("Ctrl-C is safe - progress is saved after every case.\n")

    started = time.time()
    completed = failed = 0

    for i, ec in enumerate(pending, start=1):
        if _INTERRUPTED:
            break

        prompt = build_investigation_prompt(ec.case, ec.evidence)
        pacer.acquire(estimate_tokens(prompt))

        t0 = time.time()
        result = investigate(ec.case, ec.evidence, settings)
        latency_ms = int((time.time() - t0) * 1000)

        record_result(db_path, run_id, ec.case_id, ec.ground_truth, ec.split,
                      ec.archetype, ec.case.amount, result, latency_ms)

        if result.succeeded:
            completed += 1
            mark = "ok " if (
                (result.classification != "NO_CASE") == (ec.ground_truth == "DEFENSIBLE")
            ) else "MISS"
            print(f"[{len(done)+i:>3}/{len(dataset)}] {ec.case_id}  "
                  f"{ec.archetype:<30} truth={ec.ground_truth:<13} "
                  f"ai={result.classification:<12} {mark}  {latency_ms/1000:.1f}s")
        else:
            failed += 1
            print(f"[{len(done)+i:>3}/{len(dataset)}] {ec.case_id}  "
                  f"{ec.archetype:<30} FAILED: {result.failure_reason}")

    elapsed = time.time() - started
    status = "interrupted" if _INTERRUPTED else "completed"
    mark_run(db_path, run_id, status)

    print(f"\n{completed} scored, {failed} failed, {elapsed/60:.1f} min elapsed "
          f"({pacer.stats.waits} pacing waits, {pacer.stats.seconds_waited/60:.1f} min idle)")

    if _INTERRUPTED:
        remaining = len(dataset) - len(completed_case_ids(db_path, run_id))
        print(f"\nStopped with {remaining} case(s) left. Nothing was lost. Resume with:")
        print(f"    uv run scripts/run_evaluation.py --split {args.split}")
        return 130

    return _report(db_path, run_id, settings, args.json_out)


def _report(db_path: Path, run_id: str, settings, json_out: str | None) -> int:
    results = load_results(db_path, run_id)
    if not results:
        print(f"No results stored for {run_id}. Run the evaluation first.")
        return 1

    metrics = compute_metrics(results, settings)
    save_metrics(db_path, run_id, metrics)

    m = metrics["metrics"]
    fin = metrics["financial_impact"]
    print("\n" + "=" * 72)
    print("RESULTS")
    print("=" * 72)
    print(f"scored {metrics['cases_scored']}/{metrics['cases_total']} cases "
          f"(coverage {metrics['coverage']:.0%})")
    if metrics["cases_failed"]:
        print(f"failed: {metrics['cases_failed']}  {metrics['failure_reasons']}")
    print()
    print(f"  Precision  {m['precision']:.3f}")
    print(f"  Recall     {m['recall']:.3f}")
    print(f"  F1         {m['f1']:.3f}")
    print(f"  Accuracy   {m['accuracy']:.3f}")
    print()
    print(f"  Confusion  TP {m['true_positive']:<4} FP {m['false_positive']:<4}")
    print(f"             FN {m['false_negative']:<4} TN {m['true_negative']:<4}")
    print(f"  FP rate    {m['false_positive_rate']:.3f}  "
          f"(urged a doomed contest)")
    print(f"  FN rate    {m['false_negative_rate']:.3f}  "
          f"(abandoned a winnable case)")
    print()
    print(f"  WEAK_CASE mapped as '{metrics['primary_weak_mapping']}' "
          f"({metrics['weak_case_count']} cases hinge on this)")
    alt_key = next(k for k in metrics if k.startswith("sensitivity_weak_as_"))
    alt = metrics[alt_key]
    print(f"  if mapped the other way: P {alt['precision']:.3f} / "
          f"R {alt['recall']:.3f} / F1 {alt['f1']:.3f}")
    print()
    print("  Financial exposure (assumptions - see src/config.py TUNABLES):")
    print(f"    defended (put forward)  INR {fin['amount_defended_minor']/100:>12,.2f}")
    print(f"    wrongly contested       INR {fin['amount_wrongly_contested_minor']/100:>12,.2f}")
    print(f"    missed (winnable)       INR {fin['amount_missed_minor']/100:>12,.2f}")
    print(f"    correctly dropped       INR {fin['amount_correctly_dropped_minor']/100:>12,.2f}")
    print(f"    AI cost                 INR {fin['operational_cost_minor']/100:>12,.2f}")
    print(f"    all-manual baseline     INR {fin['manual_baseline_cost_minor']/100:>12,.2f}")
    print()
    print("  Weakest archetypes:")
    worst = sorted(metrics["per_archetype"].items(), key=lambda kv: kv[1]["accuracy"])[:4]
    for name, b in worst:
        print(f"    {name:<32} {b['correct']}/{b['n']}  {b['accuracy']:.0%}")

    if json_out:
        Path(json_out).write_text(json.dumps(metrics, indent=2))
        print(f"\nmetrics JSON -> {json_out}")

        # The SQLite file is gitignored like every other *.db in this project,
        # so the per-case history would not survive a fresh clone. Export it
        # next to the metrics as CSV - diffable, portable, and enough to
        # re-derive every number above without re-running the model.
        history_path = Path(json_out).with_name(
            Path(json_out).stem.replace("_metrics", "") + "_history.csv"
        )
        with history_path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "case_id", "archetype", "ground_truth", "split", "amount_minor",
                "succeeded", "classification", "confidence", "recommended_action",
                "failure_reason", "latency_ms",
            ])
            for r in results:
                writer.writerow([
                    r.case_id, r.archetype, r.ground_truth, r.split, r.amount,
                    int(r.succeeded), r.classification or "", r.confidence or "",
                    r.recommended_action or "", r.failure_reason or "", r.latency_ms,
                ])
        print(f"case history -> {history_path} ({len(results)} rows)")

    print(f"stored in    -> {db_path} (table: eval_metrics)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
