"""Phase 8: dataset integrity, resumability, pacing, and metric correctness.

No live Groq calls - the agent is mocked. What's actually under test here is
whether the harness can be trusted: does the dataset stay reproducible, does
an interrupted run resume without losing or repeating work, and is the
arithmetic behind the headline numbers right.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.config import load_settings
from src.evaluation_dataset import (
    DATASET_VERSION,
    dataset_fingerprint,
    dataset_summary,
    generate_dataset,
)
from src.evaluation_metrics import (
    ConfusionMatrix,
    compute_metrics,
    confusion,
    financial_impact,
    predict_defensible,
)
from src.evaluation_store import (
    StoredResult,
    completed_case_ids,
    init_evaluation_db,
    load_metrics,
    load_results,
    make_run_id,
    mark_run,
    record_result,
    save_metrics,
    start_or_resume_run,
)
from src.investigation_schema import EvidenceCitation, InvestigationFailure, InvestigationResult
from src.rate_limiter import TokenBudgetPacer, estimate_tokens


@pytest.fixture()
def settings(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abcdefghijklmn")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "x")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake")
    return load_settings(require_razorpay=False)


@pytest.fixture()
def eval_db(tmp_path):
    p = tmp_path / "evaluation.db"
    init_evaluation_db(p)
    return p


def _result(classification="STRONG_CASE", succeeded=True):
    if not succeeded:
        return InvestigationFailure(
            dispute_id="d", failure_reason="AI_UNAVAILABLE", detail="down",
            investigation_timestamp=int(time.time()), attempts=1,
        )
    return InvestigationResult(
        dispute_id="d", classification=classification, confidence=0.9,
        executive_summary="s", reason="r",
        supporting_evidence=[EvidenceCitation("order", "ORD-1", "n")],
        missing_evidence=[], conflicting_evidence=[], recommended_action="CONTEST",
        risk_factors=[], investigation_timestamp=int(time.time()), model="m",
        is_simulated_case=True,
    )


def _stored(case_id, truth, classification, amount=100_000, succeeded=True, archetype="a"):
    return StoredResult(
        case_id=case_id, ground_truth=truth, split="holdout", archetype=archetype,
        succeeded=succeeded, classification=classification if succeeded else None,
        confidence=0.9 if succeeded else None,
        recommended_action="CONTEST" if succeeded else None,
        failure_reason=None if succeeded else "AI_UNAVAILABLE",
        amount=amount, latency_ms=1000, created_at=int(time.time()),
    )


# ----------------------------------------------------------------------
# dataset integrity
# ----------------------------------------------------------------------

def test_dataset_is_deterministic_across_calls():
    """A re-run must measure the same thing, or numbers aren't comparable."""
    a, b = generate_dataset(), generate_dataset()
    assert dataset_fingerprint(a) == dataset_fingerprint(b)
    assert [c.case_id for c in a] == [c.case_id for c in b]
    assert [c.ground_truth for c in a] == [c.ground_truth for c in b]
    assert [c.case.amount for c in a] == [c.case.amount for c in b]


def test_dataset_has_requested_size_and_split():
    cases = generate_dataset(total=200, holdout=50)
    assert len(cases) == 200
    assert sum(1 for c in cases if c.split == "holdout") == 50
    assert sum(1 for c in cases if c.split == "dev") == 150


def test_distribution_is_not_fifty_fifty():
    """The spec explicitly forbids an artificially balanced set."""
    summary = dataset_summary(generate_dataset())
    assert summary["defensible_pct"] != 50.0
    assert 55 <= summary["defensible_pct"] <= 70


def test_holdout_is_stratified_to_match_the_population():
    """An unstratified 50-case holdout drifts from the population and biases
    every headline number computed from it."""
    cases = generate_dataset()
    overall = dataset_summary(cases)["defensible_pct"]
    holdout = dataset_summary([c for c in cases if c.split == "holdout"])["defensible_pct"]
    assert abs(overall - holdout) <= 4.0


def test_holdout_covers_every_archetype():
    cases = generate_dataset()
    all_arch = {c.archetype for c in cases}
    holdout_arch = {c.archetype for c in cases if c.split == "holdout"}
    assert holdout_arch == all_arch


def test_every_case_has_a_label_and_a_rationale():
    for c in generate_dataset():
        assert c.ground_truth in {"DEFENSIBLE", "INDEFENSIBLE"}
        assert c.label_rationale.strip()


def test_ground_truth_never_appears_in_the_prompt():
    """The label is derived from planted facts - it must not leak into what
    the model reads, or the evaluation measures nothing."""
    from src.investigation_agent import build_investigation_prompt

    for case in generate_dataset()[:25]:
        prompt = build_investigation_prompt(case.case, case.evidence)
        assert "DEFENSIBLE" not in prompt
        assert "INDEFENSIBLE" not in prompt
        assert case.label_rationale not in prompt
        assert case.archetype not in prompt


def test_archetypes_carry_consistent_ground_truth():
    """One archetype plants one situation, so it must always yield one label."""
    by_arch: dict[str, set[str]] = {}
    for c in generate_dataset():
        by_arch.setdefault(c.archetype, set()).add(c.ground_truth)
    for archetype, labels in by_arch.items():
        assert len(labels) == 1, f"{archetype} produced mixed labels: {labels}"


def test_fingerprint_changes_when_the_dataset_changes():
    assert dataset_fingerprint(generate_dataset(total=200, holdout=50)) != \
           dataset_fingerprint(generate_dataset(total=200, holdout=40))


# ----------------------------------------------------------------------
# resumability - the property that makes a 1-hour run survivable
# ----------------------------------------------------------------------

def test_resume_skips_completed_cases(eval_db, settings):
    run_id = make_run_id(DATASET_VERSION, "holdout", "m", "fp")
    start_or_resume_run(eval_db, run_id, DATASET_VERSION, "holdout", "m", 50, {})

    record_result(eval_db, run_id, "EVAL-1", "DEFENSIBLE", "holdout", "a",
                  100_000, _result(), 1200)
    record_result(eval_db, run_id, "EVAL-2", "INDEFENSIBLE", "holdout", "b",
                  200_000, _result("NO_CASE"), 1100)

    assert completed_case_ids(eval_db, run_id) == {"EVAL-1", "EVAL-2"}


def test_second_run_is_detected_as_a_resume(eval_db):
    run_id = make_run_id(DATASET_VERSION, "holdout", "m", "fp")
    assert start_or_resume_run(eval_db, run_id, DATASET_VERSION, "holdout", "m", 50, {}) is False
    assert start_or_resume_run(eval_db, run_id, DATASET_VERSION, "holdout", "m", 50, {}) is True


def test_results_survive_an_interrupted_run(eval_db):
    """Interrupting must lose nothing already committed."""
    run_id = make_run_id(DATASET_VERSION, "holdout", "m", "fp")
    start_or_resume_run(eval_db, run_id, DATASET_VERSION, "holdout", "m", 50, {})
    record_result(eval_db, run_id, "EVAL-1", "DEFENSIBLE", "holdout", "a",
                  100_000, _result(), 1000)
    mark_run(eval_db, run_id, "interrupted")

    assert len(load_results(eval_db, run_id)) == 1
    assert completed_case_ids(eval_db, run_id) == {"EVAL-1"}


def test_failed_investigations_are_recorded_not_dropped(eval_db):
    """A crash is data too - it must not vanish and quietly inflate coverage."""
    run_id = make_run_id(DATASET_VERSION, "holdout", "m", "fp")
    start_or_resume_run(eval_db, run_id, DATASET_VERSION, "holdout", "m", 50, {})
    record_result(eval_db, run_id, "EVAL-1", "DEFENSIBLE", "holdout", "a",
                  100_000, _result(succeeded=False), 900)

    stored = load_results(eval_db, run_id)
    assert len(stored) == 1
    assert stored[0].succeeded is False
    assert stored[0].classification is None


def test_run_id_changes_with_model_and_dataset(eval_db):
    """Two models' results must never land in one confusion matrix."""
    a = make_run_id(DATASET_VERSION, "holdout", "gpt-oss-120b", "fp1")
    b = make_run_id(DATASET_VERSION, "holdout", "gpt-oss-20b", "fp1")
    c = make_run_id(DATASET_VERSION, "holdout", "gpt-oss-120b", "fp2")
    assert len({a, b, c}) == 3


def test_metrics_round_trip_through_storage(eval_db, settings):
    run_id = make_run_id(DATASET_VERSION, "holdout", "m", "fp")
    start_or_resume_run(eval_db, run_id, DATASET_VERSION, "holdout", "m", 2, {})
    metrics = {"metrics": {"f1": 0.9}, "cases_scored": 2}
    save_metrics(eval_db, run_id, metrics)
    assert load_metrics(eval_db, run_id)["metrics"]["f1"] == 0.9


# ----------------------------------------------------------------------
# rate limiting
# ----------------------------------------------------------------------

def test_pacer_allows_requests_within_budget():
    pacer = TokenBudgetPacer(tokens_per_minute=10_000, safety_margin=1.0)
    start = time.monotonic()
    for _ in range(4):
        pacer.acquire(2_000)
    assert time.monotonic() - start < 0.5
    assert pacer.stats.waits == 0


def test_pacer_blocks_when_the_budget_is_exhausted():
    pacer = TokenBudgetPacer(tokens_per_minute=1_000, safety_margin=1.0, window_seconds=1.0)
    pacer.acquire(900)
    waited = pacer.acquire(400)
    assert waited > 0.5
    assert pacer.stats.waits >= 1


def test_pacer_does_not_deadlock_on_an_oversized_request():
    """A single request bigger than the whole budget must go through and let
    the API's own limit handle it, not hang the run forever."""
    pacer = TokenBudgetPacer(tokens_per_minute=1_000, safety_margin=1.0, window_seconds=60.0)
    start = time.monotonic()
    pacer.acquire(5_000)
    assert time.monotonic() - start < 0.5


def test_token_estimate_is_conservative():
    """Under-estimating costs a 429 and a retry; over-estimating costs idle
    time. It should err high."""
    text = "word " * 1000  # ~5000 chars, realistically ~1250 tokens
    assert estimate_tokens(text, output_allowance=0) > 1250


# ----------------------------------------------------------------------
# metric correctness
# ----------------------------------------------------------------------

def test_confusion_matrix_arithmetic():
    cm = ConfusionMatrix(true_positive=70, false_positive=10,
                         false_negative=20, true_negative=100)
    assert cm.precision == pytest.approx(0.875)
    assert cm.recall == pytest.approx(0.77777, rel=1e-4)
    assert cm.f1 == pytest.approx(0.82353, rel=1e-4)
    assert cm.accuracy == pytest.approx(0.85)
    assert cm.false_positive_rate == pytest.approx(0.09091, rel=1e-4)
    assert cm.false_negative_rate == pytest.approx(0.22222, rel=1e-4)


def test_weak_case_mapping_changes_the_prediction():
    assert predict_defensible("WEAK_CASE", "defensible") is True
    assert predict_defensible("WEAK_CASE", "indefensible") is False
    assert predict_defensible("STRONG_CASE", "indefensible") is True
    assert predict_defensible("NO_CASE", "defensible") is False


def test_confusion_counts_each_quadrant_correctly():
    results = [
        _stored("1", "DEFENSIBLE", "STRONG_CASE"),      # TP
        _stored("2", "INDEFENSIBLE", "STRONG_CASE"),    # FP
        _stored("3", "DEFENSIBLE", "NO_CASE"),          # FN
        _stored("4", "INDEFENSIBLE", "NO_CASE"),        # TN
    ]
    cm = confusion(results, "defensible")
    assert (cm.true_positive, cm.false_positive, cm.false_negative, cm.true_negative) == (1, 1, 1, 1)


def test_failed_cases_are_excluded_from_the_confusion_matrix():
    """A crashed investigation isn't a correct 'no' - it's not a prediction."""
    results = [
        _stored("1", "DEFENSIBLE", "STRONG_CASE"),
        _stored("2", "INDEFENSIBLE", None, succeeded=False),
    ]
    cm = confusion(results, "defensible")
    assert cm.total == 1


def test_metrics_report_coverage_and_failures(settings):
    results = [
        _stored("1", "DEFENSIBLE", "STRONG_CASE"),
        _stored("2", "DEFENSIBLE", None, succeeded=False),
    ]
    m = compute_metrics(results, settings)
    assert m["cases_total"] == 2
    assert m["cases_scored"] == 1
    assert m["cases_failed"] == 1
    assert m["coverage"] == 0.5
    assert m["failure_reasons"] == {"AI_UNAVAILABLE": 1}


def test_metrics_report_both_weak_mappings(settings):
    """The WEAK mapping is a judgment call, so both must be shown."""
    results = [
        _stored("1", "DEFENSIBLE", "WEAK_CASE"),
        _stored("2", "INDEFENSIBLE", "WEAK_CASE"),
    ]
    m = compute_metrics(results, settings, primary_weak_mapping="defensible")
    assert m["primary_weak_mapping"] == "defensible"
    assert m["weak_case_count"] == 2
    assert "sensitivity_weak_as_indefensible" in m
    # under the primary mapping both are predicted defensible: 1 TP, 1 FP
    assert m["metrics"]["true_positive"] == 1
    assert m["metrics"]["false_positive"] == 1
    # flipped, both are predicted indefensible: 1 FN, 1 TN
    alt = m["sensitivity_weak_as_indefensible"]
    assert alt["false_negative"] == 1
    assert alt["true_negative"] == 1


def test_financial_impact_buckets_amounts_by_quadrant(settings):
    results = [
        _stored("1", "DEFENSIBLE", "STRONG_CASE", amount=500_000),    # TP
        _stored("2", "INDEFENSIBLE", "STRONG_CASE", amount=300_000),  # FP
        _stored("3", "DEFENSIBLE", "NO_CASE", amount=200_000),        # FN
        _stored("4", "INDEFENSIBLE", "NO_CASE", amount=100_000),      # TN
    ]
    fin = financial_impact(results, "defensible", settings)
    assert fin.amount_defended == 500_000
    assert fin.amount_wrongly_contested == 300_000
    assert fin.amount_missed == 200_000
    assert fin.amount_correctly_dropped == 100_000


def test_financial_impact_does_not_claim_money_recovered(settings):
    """Contesting isn't winning - the bank decides. The report must not imply
    otherwise."""
    fin = financial_impact([_stored("1", "DEFENSIBLE", "STRONG_CASE")], "defensible", settings)
    note = fin.to_dict()["note"].lower()
    assert "not money recovered" in note
    assert "assumptions" in note


def test_per_archetype_breakdown_surfaces_weak_spots(settings):
    results = [
        _stored("1", "DEFENSIBLE", "STRONG_CASE", archetype="good"),
        _stored("2", "DEFENSIBLE", "STRONG_CASE", archetype="good"),
        _stored("3", "INDEFENSIBLE", "STRONG_CASE", archetype="bad"),
        _stored("4", "INDEFENSIBLE", "STRONG_CASE", archetype="bad"),
    ]
    per = compute_metrics(results, settings)["per_archetype"]
    assert per["good"]["accuracy"] == 1.0
    assert per["bad"]["accuracy"] == 0.0


def test_empty_results_do_not_crash_the_report(settings):
    m = compute_metrics([], settings)
    assert m["cases_total"] == 0
    assert m["metrics"]["f1"] == 0.0
