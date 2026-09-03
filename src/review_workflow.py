"""Workflow logic behind the review dashboard.

Deliberately free of any Streamlit import: deadline urgency, queue
statistics and the recording of human decisions are ordinary business rules
that must be testable without rendering a UI. `dashboard/app.py` is a view
over this module.

The human-decision functions here are the ONLY place a case moves past
PENDING_HUMAN_REVIEW, and each one writes to the existing audit log.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Settings
from .database import (
    CaseRecord,
    get_audit_log,
    get_latest_investigation,
    transition_case_state,
)

Urgency = Literal["EXPIRED", "CRITICAL", "WARNING", "NORMAL"]

# Human decisions. APPROVE/REJECT map onto the spec's state machine;
# REQUEST_FURTHER_REVIEW deliberately does NOT change state - see
# record_further_review_request().
ReviewDecision = Literal["APPROVE", "REJECT", "REQUEST_FURTHER_REVIEW"]


# ----------------------------------------------------------------------
# human-readable labels
#
# A reviewer working a queue of cases with different dispute reasons and
# different pipeline states at the same time is the exact situation where
# raw enum strings (PENDING_HUMAN_REVIEW, goods_services_not_provided) cost
# real comprehension time. These map the internal values the rest of the
# codebase correctly uses onto plain language, with a safe fallback for any
# value not in the table - reason_code in particular has no closed enum
# (Razorpay does not document one - see dispute_schema.py), so an unknown
# code must degrade gracefully, not crash or disappear.
# ----------------------------------------------------------------------

# The state machine's own vocabulary (database.ALLOWED_TRANSITIONS) is
# unchanged; this only affects what a human reads.
CASE_STATE_LABELS: dict[str, str] = {
    "INGESTED": "New — not yet investigated",
    "ANALYZING": "AI investigating…",
    "ANALYSIS_COMPLETE": "Investigation complete",
    "PENDING_HUMAN_REVIEW": "Awaiting your review",
    "APPROVED": "Approved — ready to draft",
    "DRAFTED": "Draft saved at Razorpay (not submitted)",
    "SUBMITTED": "Submitted to Razorpay",
    "OVERRULED": "Rejected by reviewer",
}

# The sequential "happy path" the workflow stepper renders. OVERRULED is
# deliberately excluded - it's a stop, not a step, and is shown separately.
WORKFLOW_STEPS: list[tuple[str, str]] = [
    ("INGESTED", "Ingested"),
    ("ANALYZING", "AI Investigating"),
    ("ANALYSIS_COMPLETE", "Investigation Complete"),
    ("PENDING_HUMAN_REVIEW", "Awaiting Review"),
    ("APPROVED", "Approved"),
    ("DRAFTED", "Drafted"),
    ("SUBMITTED", "Submitted"),
]
_STEP_INDEX = {state: i for i, (state, _) in enumerate(WORKFLOW_STEPS)}

# Reason codes observed in this project (Phase 1 vendored network codes are a
# different, lower-level taxonomy - see NOTES.md N-002; these are Razorpay's
# own normalised snake_case field, which has no documented closed enum).
REASON_CODE_LABELS: dict[str, str] = {
    "goods_services_not_provided": "Customer says the product/service was never provided",
    "goods_services_not_as_described": "Customer says the item didn't match its description",
    "credit_not_processed": "Customer says a promised refund was never received",
    "subscription_canceled_but_charged": "Customer was billed after cancelling",
    "duplicate_transaction": "Customer says they were charged twice",
    "processed_invalid_expired_card": "Charge processed on an invalid or expired card",
    "unrecognized_transaction": "Customer doesn't recognise this charge",
}


def case_state_label(case_state: str) -> str:
    """Plain-language gloss for a case_state value. Never fails on an
    unknown value - falls back to a readable version of the raw string."""
    return CASE_STATE_LABELS.get(case_state, case_state.replace("_", " ").title())


def reason_code_label(reason_code: str) -> str:
    """Plain-language gloss for a Razorpay reason_code. reason_code has no
    documented closed enum, so an unrecognised code must degrade to a
    readable fallback rather than error or vanish."""
    return REASON_CODE_LABELS.get(reason_code, reason_code.replace("_", " ").capitalize())


@dataclass(frozen=True)
class WorkflowStep:
    state: str
    label: str
    is_current: bool
    is_complete: bool
    is_stopped: bool  # OVERRULED - the case will not proceed further


def workflow_progress(case_state: str) -> list[WorkflowStep]:
    """The stepper: every step on the happy path, marked complete / current
    / upcoming relative to `case_state`. If the case was OVERRULED, every
    step up to PENDING_HUMAN_REVIEW is complete and the rest is marked
    stopped, rather than silently omitted - a reviewer should be able to see
    exactly how far a rejected case got before it was stopped.
    """
    if case_state == "OVERRULED":
        current_index = _STEP_INDEX["PENDING_HUMAN_REVIEW"]
    else:
        current_index = _STEP_INDEX.get(case_state, 0)

    steps = []
    for i, (state, label) in enumerate(WORKFLOW_STEPS):
        steps.append(WorkflowStep(
            state=state, label=label,
            is_current=(i == current_index and case_state != "OVERRULED"),
            is_complete=(i < current_index) or (i == current_index and case_state == "OVERRULED"),
            is_stopped=(case_state == "OVERRULED" and i > current_index),
        ))
    return steps


@dataclass(frozen=True)
class DeadlineStatus:
    urgency: Urgency
    hours_remaining: float
    respond_by: int

    @property
    def is_expired(self) -> bool:
        return self.urgency == "EXPIRED"

    @property
    def label(self) -> str:
        if self.urgency == "EXPIRED":
            return "DEADLINE EXPIRED"
        if self.urgency == "CRITICAL":
            return f"CRITICAL - {self.hours_remaining:.0f}h left"
        if self.urgency == "WARNING":
            return f"WARNING - {self.hours_remaining:.0f}h left"
        days = self.hours_remaining / 24
        return f"{days:.0f}d left"


def deadline_status(
    respond_by: int, settings: Settings, *, now: int | None = None
) -> DeadlineStatus:
    """Classify how urgent a dispute's response deadline is.

    Thresholds come from config (Phase 1) rather than being hard-coded here,
    per the spec's requirement that they stay configurable.
    """
    now = now if now is not None else int(time.time())
    hours_remaining = (respond_by - now) / 3600.0

    if hours_remaining <= 0:
        urgency: Urgency = "EXPIRED"
    elif hours_remaining < settings.deadlines.critical_hours:
        urgency = "CRITICAL"
    elif hours_remaining < settings.deadlines.warning_hours:
        urgency = "WARNING"
    else:
        urgency = "NORMAL"

    return DeadlineStatus(urgency=urgency, hours_remaining=hours_remaining, respond_by=respond_by)


@dataclass(frozen=True)
class CaseSummary:
    """One row in the dispute queue: the case plus whatever I know about it."""

    case: CaseRecord
    classification: str | None
    confidence: float | None
    recommended_action: str | None
    investigation_failed: bool
    deadline: DeadlineStatus

    @property
    def needs_review(self) -> bool:
        return self.case.case_state in {
            "INGESTED", "ANALYZING", "ANALYSIS_COMPLETE", "PENDING_HUMAN_REVIEW",
        }

    @property
    def human_decided(self) -> bool:
        return self.case.case_state in {"APPROVED", "OVERRULED", "DRAFTED", "SUBMITTED"}


def build_case_summary(case: CaseRecord, case_db: Path, settings: Settings) -> CaseSummary:
    stored = get_latest_investigation(case_db, case.dispute_id)
    classification = confidence = action = None
    failed = False
    if stored is not None:
        if stored["succeeded"]:
            classification = stored["classification"]
            confidence = stored["confidence"]
            action = stored["recommended_action"]
        else:
            failed = True
    return CaseSummary(
        case=case, classification=classification, confidence=confidence,
        recommended_action=action, investigation_failed=failed,
        deadline=deadline_status(case.respond_by, settings),
    )


@dataclass(frozen=True)
class QueueStats:
    total: int
    awaiting_review: int
    strong: int
    weak: int
    no_case: int
    not_investigated: int
    failed_investigations: int
    total_disputed_amount: int
    currency: str
    approaching_deadline: int
    expired: int

    @property
    def amount_display(self) -> str:
        return f"{self.currency} {self.total_disputed_amount / 100:,.0f}"


def summarise_queue(summaries: list[CaseSummary]) -> QueueStats:
    """Overview metrics. Amounts are summed only where the currency matches
    the first case's - mixing currencies into one total would be misleading,
    so anything else is excluded rather than silently added."""
    currency = summaries[0].case.currency if summaries else "INR"
    return QueueStats(
        total=len(summaries),
        awaiting_review=sum(1 for s in summaries if s.needs_review),
        strong=sum(1 for s in summaries if s.classification == "STRONG_CASE"),
        weak=sum(1 for s in summaries if s.classification == "WEAK_CASE"),
        no_case=sum(1 for s in summaries if s.classification == "NO_CASE"),
        not_investigated=sum(
            1 for s in summaries if s.classification is None and not s.investigation_failed
        ),
        failed_investigations=sum(1 for s in summaries if s.investigation_failed),
        total_disputed_amount=sum(
            s.case.amount for s in summaries if s.case.currency == currency
        ),
        currency=currency,
        approaching_deadline=sum(
            1 for s in summaries if s.deadline.urgency in {"CRITICAL", "WARNING"}
        ),
        expired=sum(1 for s in summaries if s.deadline.is_expired),
    )


# ----------------------------------------------------------------------
# state progression
# ----------------------------------------------------------------------

def advance_to_review(case_db: Path, case: CaseRecord, *, actor: str = "system") -> CaseRecord:
    """Walk a case from wherever it is up to PENDING_HUMAN_REVIEW.

    Investigation (Phase 4) intentionally does not touch case state, so the
    transitions are applied here, one legal edge at a time, each audited.
    Already-reviewed cases are left alone.
    """
    record = case
    path = {
        "INGESTED": ["ANALYZING", "ANALYSIS_COMPLETE", "PENDING_HUMAN_REVIEW"],
        "ANALYZING": ["ANALYSIS_COMPLETE", "PENDING_HUMAN_REVIEW"],
        "ANALYSIS_COMPLETE": ["PENDING_HUMAN_REVIEW"],
    }.get(case.case_state, [])

    for next_state in path:
        record = transition_case_state(
            case_db, record.dispute_id, next_state, actor=actor,
            action="investigation_progress",
            reason="AI investigation completed; awaiting human review",
        )
    return record


def record_human_decision(
    case_db: Path,
    dispute_id: str,
    decision: ReviewDecision,
    *,
    reviewer: str,
    reason: str,
    ai_classification: str | None = None,
) -> CaseRecord | None:
    """Record a reviewer's decision, with the AI's recommendation alongside it.

    The audit reason always captures BOTH what the AI recommended and what
    the human decided, so a later reader can see where a human overruled the
    model rather than only seeing the final state.
    """
    context = f"AI recommended {ai_classification or 'nothing'}; human {decision}. {reason}".strip()

    if decision == "APPROVE":
        return transition_case_state(
            case_db, dispute_id, "APPROVED", actor=reviewer,
            action="human_approve", reason=context,
        )
    if decision == "REJECT":
        return transition_case_state(
            case_db, dispute_id, "OVERRULED", actor=reviewer,
            action="human_reject", reason=context,
        )
    return record_further_review_request(
        case_db, dispute_id, reviewer=reviewer, reason=context
    )


def record_further_review_request(
    case_db: Path, dispute_id: str, *, reviewer: str, reason: str
) -> None:
    """Log a "needs more work" note WITHOUT changing state.

    The spec's state machine has no state for "a human looked and wants more
    information" - and inventing one would put the case somewhere none of the
    existing transitions expect. The case stays in PENDING_HUMAN_REVIEW,
    which is already true, and the request is recorded in the audit log.
    """
    from .database import connection

    with connection(case_db) as conn:
        row = conn.execute(
            "SELECT case_state FROM cases WHERE dispute_id = ?", (dispute_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no case for dispute_id={dispute_id!r}")
        state = row["case_state"]
        conn.execute(
            "INSERT INTO audit_log (dispute_id, previous_state, new_state, actor, "
            "action, reason, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dispute_id, state, state, reviewer, "human_request_further_review",
             reason, int(time.time())),
        )
    return None


def review_history(case_db: Path, dispute_id: str) -> list[dict]:
    return get_audit_log(case_db, dispute_id)
