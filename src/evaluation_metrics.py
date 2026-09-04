"""Scoring an evaluation run.

Positive class is DEFENSIBLE (spec section 18), so:

    TP  AI said defensible, case really was  -> rightly contested
    FP  AI said defensible, case was not     -> merchant wastes a contest
    FN  AI said indefensible, case was       -> winnable money left on table
    TN  AI said indefensible, case was not   -> rightly dropped

The one genuinely debatable decision is WEAK_CASE. STRONG_CASE clearly maps
to defensible and NO_CASE to indefensible, but WEAK sits between them - it
means "there's something here, with real gaps", and its recommended_action is
usually MANUAL_REVIEW. Mapping it either way changes the numbers, so this
module refuses to quietly pick one: it reports the primary mapping AND the
alternative, and states how many cases actually hinge on it. Anything else
would be presenting a modelling choice as a result.

Failed investigations are NOT silently dropped or counted as a prediction.
An AI that crashes on 50 cases has not "correctly declined" them - those are
reported separately, and the coverage figure says what fraction of the set
the model actually produced a usable answer for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config import Settings
from .evaluation_store import StoredResult

WeakMapping = Literal["defensible", "indefensible"]


@dataclass(frozen=True)
class ConfusionMatrix:
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int

    @property
    def total(self) -> int:
        return (self.true_positive + self.false_positive
                + self.false_negative + self.true_negative)

    @property
    def precision(self) -> float:
        denom = self.true_positive + self.false_positive
        return self.true_positive / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positive + self.false_negative
        return self.true_positive / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        return ((self.true_positive + self.true_negative) / self.total) if self.total else 0.0

    @property
    def false_positive_rate(self) -> float:
        """FP / all actually-indefensible. How often it urges a doomed contest."""
        denom = self.false_positive + self.true_negative
        return self.false_positive / denom if denom else 0.0

    @property
    def false_negative_rate(self) -> float:
        """FN / all actually-defensible. How often it abandons a winnable case."""
        denom = self.false_negative + self.true_positive
        return self.false_negative / denom if denom else 0.0

    def to_dict(self) -> dict:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "true_negative": self.true_negative,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "accuracy": round(self.accuracy, 4),
            "false_positive_rate": round(self.false_positive_rate, 4),
            "false_negative_rate": round(self.false_negative_rate, 4),
        }


def predict_defensible(classification: str, weak_as: WeakMapping) -> bool:
    if classification == "STRONG_CASE":
        return True
    if classification == "NO_CASE":
        return False
    if classification == "WEAK_CASE":
        return weak_as == "defensible"
    raise ValueError(f"unknown classification {classification!r}")


def confusion(results: list[StoredResult], weak_as: WeakMapping) -> ConfusionMatrix:
    """Only scored on cases the model actually answered."""
    tp = fp = fn = tn = 0
    for r in results:
        if not r.succeeded or r.classification is None:
            continue
        predicted = predict_defensible(r.classification, weak_as)
        actual = r.ground_truth == "DEFENSIBLE"
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif not predicted and actual:
            fn += 1
        else:
            tn += 1
    return ConfusionMatrix(tp, fp, fn, tn)


@dataclass(frozen=True)
class FinancialImpact:
    """Money at stake, under clearly-labelled assumptions.

    Deliberately NOT called "money saved". Contesting a defensible dispute
    does not guarantee winning it - the issuing bank decides, and this project
    has no win-rate data to model that with. So a true positive is counted as
    amount *defended* (put forward for contest), not amount recovered. Anyone
    reading these numbers should treat them as exposure, not P&L.
    """

    amount_defended: int          # TP - correctly put forward
    amount_wrongly_contested: int # FP - contest effort spent on unwinnable cases
    amount_missed: int            # FN - winnable, but the AI said drop it
    amount_correctly_dropped: int # TN - correctly not pursued
    operational_cost: int         # what running this cost, per the config model
    manual_baseline_cost: int     # what reviewing all of them by hand would cost
    currency: str = "INR"

    def to_dict(self) -> dict:
        return {
            "amount_defended_minor": self.amount_defended,
            "amount_wrongly_contested_minor": self.amount_wrongly_contested,
            "amount_missed_minor": self.amount_missed,
            "amount_correctly_dropped_minor": self.amount_correctly_dropped,
            "operational_cost_minor": self.operational_cost,
            "manual_baseline_cost_minor": self.manual_baseline_cost,
            "currency": self.currency,
            "note": (
                "amount_defended is money put forward for contest, NOT money "
                "recovered - the issuing bank decides the outcome and no "
                "win-rate is modelled here. Cost figures come from the "
                "TUNABLES block in src/config.py and are assumptions, not "
                "published Razorpay fees."
            ),
        }


def financial_impact(
    results: list[StoredResult], weak_as: WeakMapping, settings: Settings
) -> FinancialImpact:
    defended = wrongly = missed = dropped = 0
    scored = 0
    for r in results:
        if not r.succeeded or r.classification is None:
            continue
        scored += 1
        predicted = predict_defensible(r.classification, weak_as)
        actual = r.ground_truth == "DEFENSIBLE"
        if predicted and actual:
            defended += r.amount
        elif predicted and not actual:
            wrongly += r.amount
        elif not predicted and actual:
            missed += r.amount
        else:
            dropped += r.amount

    # Rupees -> paise, since every amount in this project is in minor units.
    ai_cost = settings.costs.ai_investigation_inr * 100 * scored
    manual_cost = settings.costs.manual_review_inr * 100 * scored
    return FinancialImpact(
        amount_defended=defended,
        amount_wrongly_contested=wrongly,
        amount_missed=missed,
        amount_correctly_dropped=dropped,
        operational_cost=ai_cost,
        manual_baseline_cost=manual_cost,
    )


def per_archetype_accuracy(results: list[StoredResult], weak_as: WeakMapping) -> dict:
    """Where it goes wrong, not just how often - one weak archetype hiding
    inside a good average is exactly what a single F1 number conceals."""
    buckets: dict[str, dict] = {}
    for r in results:
        if not r.succeeded or r.classification is None:
            continue
        b = buckets.setdefault(r.archetype, {"n": 0, "correct": 0, "ground_truth": r.ground_truth})
        b["n"] += 1
        predicted = predict_defensible(r.classification, weak_as)
        if predicted == (r.ground_truth == "DEFENSIBLE"):
            b["correct"] += 1
    return {
        name: {
            "n": b["n"],
            "correct": b["correct"],
            "accuracy": round(b["correct"] / b["n"], 4) if b["n"] else 0.0,
            "ground_truth": b["ground_truth"],
        }
        for name, b in sorted(buckets.items())
    }


def compute_metrics(
    results: list[StoredResult],
    settings: Settings,
    primary_weak_mapping: WeakMapping = "defensible",
) -> dict:
    """The full report for one run."""
    total = len(results)
    succeeded = [r for r in results if r.succeeded and r.classification is not None]
    failures = [r for r in results if not r.succeeded]
    failure_reasons: dict[str, int] = {}
    for r in failures:
        key = r.failure_reason or "UNKNOWN"
        failure_reasons[key] = failure_reasons.get(key, 0) + 1

    classifications: dict[str, int] = {}
    for r in succeeded:
        classifications[r.classification] = classifications.get(r.classification, 0) + 1

    alternative: WeakMapping = (
        "indefensible" if primary_weak_mapping == "defensible" else "defensible"
    )
    primary_cm = confusion(results, primary_weak_mapping)
    alt_cm = confusion(results, alternative)

    confidences = [r.confidence for r in succeeded if r.confidence is not None]
    latencies = sorted(r.latency_ms for r in results)

    return {
        "cases_total": total,
        "cases_scored": len(succeeded),
        "cases_failed": len(failures),
        "coverage": round(len(succeeded) / total, 4) if total else 0.0,
        "failure_reasons": failure_reasons,
        "classification_counts": classifications,
        "weak_case_count": classifications.get("WEAK_CASE", 0),
        "primary_weak_mapping": primary_weak_mapping,
        "metrics": primary_cm.to_dict(),
        "sensitivity_weak_as_" + alternative: alt_cm.to_dict(),
        "financial_impact": financial_impact(results, primary_weak_mapping, settings).to_dict(),
        "per_archetype": per_archetype_accuracy(results, primary_weak_mapping),
        "mean_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
        "latency_ms": {
            "median": latencies[len(latencies) // 2] if latencies else 0,
            "max": latencies[-1] if latencies else 0,
        },
    }
