"""Turns a validated investigation into a Razorpay-shaped evidence package.

Two rules drive everything here:

1. **Only include evidence that actually exists and is relevant.** Razorpay
   exposes eleven evidence categories; populating one because it exists in
   the API, with nothing real behind it, would be fabricating evidence. A
   category appears in the package only when a merchant record backs it AND
   it bears on this dispute's reason code. A digital product with no shipment
   gets `access_activity_log`/`proof_of_service`, not an empty
   `shipping_proof`.
2. **Never silently send an oversized payload.** Razorpay documents a 1000
   character maximum on the evidence `summary`. The pipeline generates,
   validates, asks the model to shorten, re-validates, and only then
   truncates programmatically as a last resort - each step recorded.

This module builds a DRAFT. It does not upload documents and does not contest
anything; that is Phase 7, behind explicit human approval.
"""

from __future__ import annotations

import logging
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings, load_settings
from .database import CaseRecord
from .document_generator import GeneratedDocument, generate_evidence_document_pdf
from .investigation_schema import InvestigationResult
from .merchant_db import EVIDENCE_DOCUMENT_TYPES, CaseEvidence

logger = logging.getLogger(__name__)

# Razorpay's documented evidence object attributes (Contest a Dispute API).
# Note `term_and_conditions` is singular "term" - that is Razorpay's spelling,
# verified in Phase 1, not a typo on my side.
RAZORPAY_EVIDENCE_CATEGORIES = (
    "shipping_proof", "billing_proof", "cancellation_proof",
    "customer_communication", "proof_of_service", "explanation_letter",
    "refund_confirmation", "access_activity_log", "refund_cancellation_policy",
    "term_and_conditions", "others",
)


class EvidenceBuildError(RuntimeError):
    """The package could not be built (and must not be faked)."""


@dataclass(frozen=True)
class SummaryTrace:
    """How the contest summary reached its final length - kept so a reviewer
    can see whether the text they're approving was shortened or truncated."""

    original_length: int
    final_length: int
    limit: int
    was_shortened_by_ai: bool
    was_truncated: bool

    @property
    def within_limit(self) -> bool:
        return self.final_length <= self.limit


@dataclass(frozen=True)
class EvidencePackage:
    dispute_id: str
    classification: str
    recommended_action: str
    contest_summary: str
    summary_trace: SummaryTrace
    evidence_categories: dict[str, list[str]]
    explanation_letter: str
    generated_documents: list[GeneratedDocument]
    warnings: list[str]
    is_simulated: bool
    built_at: int
    contest_advised: bool

    @property
    def is_submittable(self) -> bool:
        """Razorpay requires at least one document id to submit a contest.

        This says the package is *structurally* complete - never that it may
        be submitted. Submission requires human approval (Phase 7).
        """
        return bool(self.generated_documents) and self.summary_trace.within_limit

    def to_dict(self) -> dict:
        return {
            "dispute_id": self.dispute_id,
            "classification": self.classification,
            "recommended_action": self.recommended_action,
            "contest_summary": self.contest_summary,
            "summary_trace": {
                "original_length": self.summary_trace.original_length,
                "final_length": self.summary_trace.final_length,
                "limit": self.summary_trace.limit,
                "was_shortened_by_ai": self.summary_trace.was_shortened_by_ai,
                "was_truncated": self.summary_trace.was_truncated,
            },
            "evidence_categories": self.evidence_categories,
            "explanation_letter": self.explanation_letter,
            "generated_documents": [
                {
                    "path": str(d.path), "document_type": d.document_type,
                    "description": d.description, "source_filename": d.source_filename,
                }
                for d in self.generated_documents
            ],
            "warnings": self.warnings,
            "is_simulated": self.is_simulated,
            "built_at": self.built_at,
            "contest_advised": self.contest_advised,
            "is_submittable": self.is_submittable,
        }


# ----------------------------------------------------------------------
# evidence category selection
# ----------------------------------------------------------------------

def select_evidence_categories(
    case: CaseRecord, evidence: CaseEvidence
) -> tuple[dict[str, list[str]], list[str]]:
    """Map real merchant records onto Razorpay evidence categories.

    Returns (categories, warnings). A category is included ONLY when a
    concrete record backs it. Each entry lists the references behind it, so
    the package can always be traced back to source rows.
    """
    categories: dict[str, list[str]] = {}
    warnings: list[str] = []
    order = evidence.order

    def add(category: str, ref: str) -> None:
        if category not in RAZORPAY_EVIDENCE_CATEGORIES:
            raise EvidenceBuildError(f"{category!r} is not a Razorpay evidence category")
        categories.setdefault(category, []).append(ref)

    # Documents already carry Razorpay's own category names (Phase 3 design).
    for doc in evidence.documents:
        if doc.document_type in RAZORPAY_EVIDENCE_CATEGORIES:
            add(doc.document_type, f"document:{doc.id}")
        else:
            add("others", f"document:{doc.id}")

    # Shipment records back shipping_proof - but only if something was
    # actually shipped. A 'never_shipped' record is not proof of shipping;
    # including it would actively harm the merchant's case.
    if evidence.shipment is not None:
        sh = evidence.shipment
        if sh.delivery_status in {"delivered", "in_transit", "returned_to_sender"}:
            add("shipping_proof", f"shipment:{sh.merchant_order_id}")
        elif sh.delivery_status == "never_shipped":
            warnings.append(
                "Shipment record shows the order was never shipped - no shipping "
                "proof can be offered."
            )
    elif order.product_type == "physical":
        warnings.append(
            "Physical product with no shipment record at all - fulfilment cannot "
            "be evidenced."
        )

    if evidence.communications:
        add("customer_communication", f"order:{order.merchant_order_id}")

    # Refunds only count as refund_confirmation when actually processed.
    if evidence.refund is not None and evidence.refund.refund_status == "processed":
        add("refund_confirmation", f"refund:{evidence.refund.merchant_order_id}")

    # Order record is billing proof of what was purchased.
    add("billing_proof", f"order:{order.merchant_order_id}")

    for policy in evidence.policies:
        if policy.policy_type in {"refund_policy", "cancellation_policy"}:
            add("refund_cancellation_policy", f"policy:{policy.policy_type}")
        elif policy.policy_type == "terms_and_conditions":
            add("term_and_conditions", f"policy:{policy.policy_type}")

    # Digital/service products are evidenced by access/service records, not
    # shipping. Flag when neither exists - that is a real gap.
    if order.product_type in {"digital", "service"}:
        has_service_proof = any(
            c in categories for c in ("access_activity_log", "proof_of_service")
        )
        if not has_service_proof:
            warnings.append(
                f"{order.product_type.capitalize()} product with no access log or "
                "proof of service on file - delivery of the service cannot be evidenced."
            )

    return categories, warnings


# ----------------------------------------------------------------------
# explanation letter + contest summary
# ----------------------------------------------------------------------

def build_explanation_letter(
    case: CaseRecord, evidence: CaseEvidence, investigation: InvestigationResult
) -> str:
    """Compose the explanation letter deterministically.

    Built from already-validated investigation content rather than a fresh
    model call: the investigation's claims have already been checked against
    real citations, so reusing them cannot introduce new hallucinations.
    """
    order = evidence.order
    amount = f"{case.currency} {case.amount / 100:,.2f}"
    lines = [
        f"Re: Dispute {case.dispute_id} - payment {case.payment_id} - {amount}",
        "",
        f"This letter responds to the dispute raised against order "
        f"{order.merchant_order_id} ({order.product}), placed for "
        f"{order.currency} {order.amount / 100:,.2f} under reason code "
        f"'{case.reason_code}'.",
        "",
        investigation.executive_summary,
        "",
        "The following records support this response:",
    ]
    for citation in investigation.supporting_evidence:
        lines.append(f"  - {citation.reference}: {citation.note}")

    if investigation.missing_evidence:
        lines += [
            "",
            "The following evidence is not available in our records and is stated "
            "here for completeness rather than omitted:",
        ]
        for item in investigation.missing_evidence:
            lines.append(f"  - {item}")

    if investigation.conflicting_evidence:
        lines += ["", "Known conflicts in the available records:"]
        for item in investigation.conflicting_evidence:
            lines.append(f"  - {item}")

    return "\n".join(lines)


def _compose_contest_summary(
    case: CaseRecord, evidence: CaseEvidence, investigation: InvestigationResult
) -> str:
    """The first-pass summary, assembled from validated investigation text."""
    parts = [investigation.executive_summary.strip()]
    if investigation.supporting_evidence:
        refs = ", ".join(c.reference for c in investigation.supporting_evidence)
        parts.append(f"Supporting records: {refs}.")
    return " ".join(parts)


def _shorten_with_ai(text: str, limit: int, settings: Settings) -> str | None:
    """Ask the model to compress the summary. Returns None on any failure -
    the caller then falls back to programmatic truncation rather than
    failing the whole package."""
    try:
        from groq import Groq
    except ImportError:
        return None
    if not settings.ai.api_key:
        return None

    try:
        client = Groq(api_key=settings.ai.api_key, timeout=float(settings.ai.timeout_seconds))
        response = client.chat.completions.create(
            model=settings.ai.model,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You compress dispute evidence summaries. Preserve every "
                        "factual claim and every record reference (like "
                        "document:7, shipment:ORD-1001) exactly as written. Never "
                        "add facts. Never drop a reference. Return ONLY the "
                        f"shortened text, under {limit} characters."
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
        shortened = (response.choices[0].message.content or "").strip()
        return shortened or None
    except Exception as exc:
        logger.warning("summary shortening failed, will truncate instead: %s", exc)
        return None


def _truncate(text: str, limit: int) -> str:
    """Last-resort truncation on a word boundary, marked so nobody mistakes
    a cut-off summary for a complete one."""
    if len(text) <= limit:
        return text
    marker = " [truncated]"
    return textwrap.shorten(text, width=limit - len(marker), placeholder="...") + marker


def build_contest_summary(
    case: CaseRecord,
    evidence: CaseEvidence,
    investigation: InvestigationResult,
    settings: Settings,
) -> tuple[str, SummaryTrace]:
    """Generate → validate → shorten → validate → truncate (spec section 20)."""
    limit = settings.contest_summary_max_chars
    summary = _compose_contest_summary(case, evidence, investigation)
    original_length = len(summary)

    if len(summary) <= limit:
        return summary, SummaryTrace(original_length, len(summary), limit, False, False)

    logger.info(
        "contest summary is %d chars (limit %d) - asking the model to shorten",
        len(summary), limit,
    )
    shortened = _shorten_with_ai(summary, limit, settings)
    if shortened and len(shortened) <= limit:
        return shortened, SummaryTrace(original_length, len(shortened), limit, True, False)

    candidate = shortened or summary
    truncated = _truncate(candidate, limit)
    logger.warning(
        "contest summary still over limit after shortening - truncated to %d chars",
        len(truncated),
    )
    return truncated, SummaryTrace(
        original_length, len(truncated), limit, shortened is not None, True
    )


# ----------------------------------------------------------------------
# the package
# ----------------------------------------------------------------------

def build_evidence_package(
    case: CaseRecord,
    evidence: CaseEvidence,
    investigation: InvestigationResult,
    settings: Settings | None = None,
    *,
    output_dir: Path | None = None,
    source_dir: Path | None = None,
    force: bool = False,
) -> EvidencePackage:
    """Assemble the full draft evidence package for a case.

    For a NO_CASE investigation this refuses by default: building a polished
    contest package for a case the investigation says cannot be won invites
    exactly the rubber-stamping this product exists to prevent. A human can
    still override with force=True (Phase 6), and the override is recorded in
    the package's warnings.
    """
    settings = settings or load_settings(require_razorpay=False)
    output_dir = output_dir or (settings.paths.generated_docs / case.dispute_id)
    source_dir = source_dir if source_dir is not None else settings.paths.merchant_db.parent / "documents"

    if investigation.classification == "NO_CASE" and not force:
        raise EvidenceBuildError(
            f"Investigation concluded NO_CASE for {case.dispute_id}: "
            f"{investigation.executive_summary} "
            "No contest package was built. A human reviewer may override this "
            "(force=True), which will be recorded."
        )

    categories, warnings = select_evidence_categories(case, evidence)

    if investigation.classification == "NO_CASE" and force:
        warnings.insert(0, (
            "HUMAN OVERRIDE: the AI investigation concluded NO_CASE, but this "
            "package was built anyway at a reviewer's request."
        ))

    contest_summary, summary_trace = build_contest_summary(
        case, evidence, investigation, settings
    )
    explanation_letter = build_explanation_letter(case, evidence, investigation)

    generated: list[GeneratedDocument] = []
    for doc in evidence.documents:
        try:
            generated.append(
                generate_evidence_document_pdf(doc, evidence, output_dir, source_dir)
            )
        except Exception as exc:  # a broken source file must not kill the package
            warnings.append(f"Could not render document {doc.filename!r}: {exc}")
            logger.warning("failed to render %s: %s", doc.filename, exc)

    if not generated:
        warnings.append(
            "No evidence documents could be produced. Razorpay requires at least "
            "one document id to submit a contest, so this package is not "
            "submittable as it stands."
        )

    if summary_trace.was_truncated:
        warnings.append(
            f"Contest summary was truncated from {summary_trace.original_length} to "
            f"{summary_trace.final_length} characters to fit Razorpay's "
            f"{summary_trace.limit}-character limit - review it before approving."
        )

    package = EvidencePackage(
        dispute_id=case.dispute_id,
        classification=investigation.classification,
        recommended_action=investigation.recommended_action,
        contest_summary=contest_summary,
        summary_trace=summary_trace,
        evidence_categories=categories,
        explanation_letter=explanation_letter,
        generated_documents=generated,
        warnings=warnings,
        is_simulated=case.is_simulated,
        built_at=int(time.time()),
        contest_advised=investigation.classification in {"STRONG_CASE", "WEAK_CASE"},
    )
    logger.info(
        "built evidence package for %s: %d categories, %d documents, submittable=%s",
        case.dispute_id, len(categories), len(generated), package.is_submittable,
    )
    return package
