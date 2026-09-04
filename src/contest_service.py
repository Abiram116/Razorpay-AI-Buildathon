"""Razorpay contest drafting and submission (Phase 7).

The only module in the project that can mutate a dispute at Razorpay, and it
is deliberately narrow:

    build_local_draft()      no network. Always safe. What a human inspects.
    save_draft_to_razorpay() PATCH .../contest with action="draft"
    submit_contest()         PATCH .../contest with action="submit"

Three hard safety properties, enforced here in code rather than in the UI:

1. **A simulated dispute can never reach Razorpay.** `assert_submittable()`
   blocks it at the service layer. The dashboard also says so, but the block
   does not depend on the dashboard.
2. **Submission requires an explicit human confirmation argument.** There is
   no default that submits; a caller must pass `human_confirmed=True` and a
   reviewer name. No code path anywhere goes from a STRONG_CASE verdict to a
   submission on its own.
3. **A failed API call never advances case state.** The state transition
   happens only after Razorpay returns success, so a network failure leaves
   the case exactly where it was, retryable.

Uploads are idempotent: a document already uploaded for this dispute reuses
its stored `doc_...` id instead of creating a duplicate at Razorpay.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .database import (
    CaseRecord,
    get_uploaded_documents,
    record_contest_attempt,
    record_uploaded_document,
    transition_case_state,
)
from .document_generator import (
    GeneratedDocument,
    generate_billing_proof_pdf,
    generate_communications_transcript_pdf,
    generate_explanation_letter_pdf,
    generate_policy_pdf,
)
from .evidence_builder import RAZORPAY_EVIDENCE_CATEGORIES, EvidencePackage
from .investigation_schema import InvestigationResult
from .merchant_db import CaseEvidence
from .razorpay_client import (
    RazorpayAuthError,
    RazorpayClient,
    RazorpayRequestError,
    RazorpayUnavailable,
)

logger = logging.getLogger(__name__)

# Categories that take a plain list of document ids. `others` is different -
# Razorpay documents it as a list of {type, document_ids} objects.
_LIST_CATEGORIES = tuple(c for c in RAZORPAY_EVIDENCE_CATEGORIES if c != "others")


class SubmissionBlocked(RuntimeError):
    """This dispute must not be sent to Razorpay."""


class ContestError(RuntimeError):
    """The contest call failed. Case state is unchanged."""


@dataclass(frozen=True)
class ContestDraft:
    """The exact payload I'd send, plus what could not be included."""

    dispute_id: str
    payload: dict[str, Any]
    document_ids_by_category: dict[str, list[str]]
    uploadable_documents: list[GeneratedDocument]
    unsupported_categories: dict[str, list[str]]
    is_simulated: bool
    blocked_reason: str | None

    @property
    def can_submit(self) -> bool:
        """Structurally submittable. A human still has to decide."""
        return self.blocked_reason is None and bool(self.document_ids_by_category)


def assert_submittable(case: CaseRecord, *, now: int | None = None) -> None:
    """Backend guard. Raises unless this dispute may legitimately be sent.

    A simulated dispute has no counterpart at Razorpay - its id is
    `sim_disp_...`, which the client's id validation would reject anyway.
    Blocking it explicitly here, with a clear reason, is better than relying
    on a downstream validation error to accidentally do the right thing.

    Found while building the Phase 9 failure demo: the dashboard only ever
    showed a "DEADLINE EXPIRED" banner (dashboard/app.py) - it never actually
    disabled the submit button, and this function never checked the deadline
    at all. A human could still click submit on a case whose respond_by had
    already passed. Razorpay would very likely reject that submission on its
    own, but this code shouldn't attempt an action it already knows is
    pointless - that's the same "fail before the network call, not after"
    principle the summary-length and simulated-dispute checks already use.
    """
    if case.is_simulated:
        raise SubmissionBlocked(
            f"{case.dispute_id} is a SIMULATED test dispute. It does not exist at "
            "Razorpay and can never be contested there. Nothing was sent."
        )
    if case.source != "razorpay_webhook":
        raise SubmissionBlocked(
            f"{case.dispute_id} did not arrive from a verified Razorpay webhook "
            f"(source={case.source!r}). Refusing to contest it."
        )
    now = now if now is not None else int(time.time())
    if case.respond_by <= now:
        raise SubmissionBlocked(
            f"{case.dispute_id}'s response deadline (respond_by) has already "
            "passed. Razorpay will not accept a contest after the deadline, "
            "so this is refused before any API call is made. Route to manual "
            "handling."
        )


def collect_uploadable_documents(
    case: CaseRecord,
    evidence: CaseEvidence,
    package: EvidencePackage,
    investigation: InvestigationResult,
    output_dir: Path,
) -> list[GeneratedDocument]:
    """Everything in the package that can actually become a Razorpay document.

    The package's `evidence_categories` map cites internal records
    (`order:ORD-1001`, `policy:refund_policy`), but Razorpay's contest fields
    take uploaded document ids - a record is not a file. So the merchant
    documents are joined here with artefacts that have real content but no
    PDF yet: the explanation letter, `billing_proof` (the order record - cited
    for every case per evidence_builder.select_evidence_categories), and any
    policy the package cites. Without this step those categories would
    silently vanish from the payload - as billing_proof in fact did until this
    was caught by inspecting the dashboard's own "cited but not sendable"
    warning on a real case.
    """
    documents = list(package.generated_documents)

    documents.append(
        generate_explanation_letter_pdf(
            case, evidence, investigation, package.explanation_letter, output_dir
        )
    )

    if "billing_proof" in package.evidence_categories:
        documents.append(generate_billing_proof_pdf(evidence, case, output_dir))

    if "customer_communication" in package.evidence_categories and evidence.communications:
        documents.append(generate_communications_transcript_pdf(evidence, case, output_dir))

    cited_policies = {
        ref.split(":", 1)[1]
        for refs in package.evidence_categories.values()
        for ref in refs
        if ref.startswith("policy:")
    }
    for policy in evidence.policies:
        if policy.policy_type in cited_policies:
            documents.append(generate_policy_pdf(policy, case, output_dir))

    return documents


def _category_for(document_type: str) -> str:
    return document_type if document_type in RAZORPAY_EVIDENCE_CATEGORIES else "others"


def upload_documents(
    client: RazorpayClient,
    case_db: Path,
    dispute_id: str,
    documents: list[GeneratedDocument],
) -> dict[str, list[str]]:
    """Upload each document, reusing anything already uploaded.

    Returns document ids grouped by Razorpay evidence category. Each success
    is persisted immediately, so a failure part-way through does not lose the
    ids already obtained - the retry picks up where it stopped.
    """
    already = get_uploaded_documents(case_db, dispute_id)
    by_category: dict[str, list[str]] = {}

    for document in documents:
        path_key = str(document.path)
        category = _category_for(document.document_type)

        if path_key in already:
            document_id = already[path_key]["razorpay_document_id"]
            logger.info("reusing already-uploaded document %s", document_id)
        else:
            if not document.path.exists():
                raise ContestError(f"document {document.path.name} no longer exists on disk")
            response = client.upload_evidence_document(path_key, document.mime_type)
            document_id = response.get("id")
            if not document_id:
                raise ContestError(
                    f"Razorpay accepted {document.path.name} but returned no document id"
                )
            record_uploaded_document(
                case_db, dispute_id, path_key, document.document_type, document_id
            )
            logger.info("uploaded %s as %s", document.path.name, document_id)

        by_category.setdefault(category, []).append(document_id)

    return by_category


def build_contest_payload(
    summary: str,
    document_ids_by_category: dict[str, list[str]],
    action: str,
    settings: Settings,
    amount: int | None = None,
) -> dict[str, Any]:
    """Assemble the exact PATCH body for Razorpay's Contest a Dispute API.

    Only documented fields are emitted, and only categories that actually
    have document ids - Razorpay is never sent an empty evidence field.
    """
    if action not in {"draft", "submit"}:
        raise ValueError("action must be 'draft' or 'submit'")

    limit = settings.contest_summary_max_chars
    if len(summary) > limit:
        # Phase 5 should already have handled this; refusing here means an
        # oversized payload can never reach Razorpay by another route.
        raise ContestError(
            f"contest summary is {len(summary)} characters, over Razorpay's "
            f"{limit}-character limit. Refusing to send."
        )

    payload: dict[str, Any] = {"summary": summary, "action": action}
    if amount is not None:
        payload["amount"] = amount

    others: list[dict[str, Any]] = []
    for category, document_ids in document_ids_by_category.items():
        if not document_ids:
            continue
        if category in _LIST_CATEGORIES:
            payload[category] = document_ids
        else:
            others.append({"type": "supporting_document", "document_ids": document_ids})
    if others:
        payload["others"] = others

    return payload


def build_local_draft(
    case: CaseRecord,
    evidence: CaseEvidence,
    package: EvidencePackage,
    investigation: InvestigationResult,
    settings: Settings | None = None,
    *,
    output_dir: Path | None = None,
) -> ContestDraft:
    """Build the draft WITHOUT contacting Razorpay.

    Always safe to call, including for simulated disputes - which is the
    point: a reviewer can inspect exactly what would be sent before anything
    is sent, and a simulated case can be demonstrated end-to-end without ever
    touching the API.
    """
    settings = settings or load_settings(require_razorpay=False)
    output_dir = output_dir or (settings.paths.generated_docs / case.dispute_id)

    documents = collect_uploadable_documents(
        case, evidence, package, investigation, output_dir
    )

    # Placeholder ids: the real ones only exist after upload. Marked clearly
    # so a preview can never be mistaken for a submitted payload.
    placeholder_ids: dict[str, list[str]] = {}
    for document in documents:
        placeholder_ids.setdefault(_category_for(document.document_type), []).append(
            f"<doc id pending upload: {document.path.name}>"
        )

    unsupported = {
        category: refs
        for category, refs in package.evidence_categories.items()
        if category not in placeholder_ids
    }

    blocked_reason = None
    try:
        assert_submittable(case)
    except SubmissionBlocked as exc:
        blocked_reason = str(exc)

    payload = build_contest_payload(
        package.contest_summary, placeholder_ids, "draft", settings, amount=case.amount
    )

    return ContestDraft(
        dispute_id=case.dispute_id,
        payload=payload,
        document_ids_by_category=placeholder_ids,
        uploadable_documents=documents,
        unsupported_categories=unsupported,
        is_simulated=case.is_simulated,
        blocked_reason=blocked_reason,
    )


def _send_contest(
    case: CaseRecord,
    evidence: CaseEvidence,
    package: EvidencePackage,
    investigation: InvestigationResult,
    action: str,
    actor: str,
    settings: Settings,
    output_dir: Path | None,
    client: RazorpayClient | None,
) -> dict[str, Any]:
    """Shared path for draft and submit. Never advances state on failure."""
    assert_submittable(case)

    settings = settings or load_settings(require_razorpay=True)
    output_dir = output_dir or (settings.paths.generated_docs / case.dispute_id)
    client = client or RazorpayClient(settings)

    documents = collect_uploadable_documents(
        case, evidence, package, investigation, output_dir
    )

    payload: dict[str, Any] = {}
    try:
        document_ids = upload_documents(client, settings.paths.case_db, case.dispute_id, documents)
        if not document_ids:
            raise ContestError(
                "Razorpay requires at least one document id to contest a dispute, "
                "and none could be uploaded."
            )
        payload = build_contest_payload(
            package.contest_summary, document_ids, action, settings, amount=case.amount
        )
        response = client.contest_dispute(case.dispute_id, payload)
    except (RazorpayUnavailable, RazorpayRequestError, RazorpayAuthError, ContestError) as exc:
        record_contest_attempt(
            settings.paths.case_db, case.dispute_id, action, False, actor,
            payload or {"action": action}, error=str(exc),
        )
        logger.error("contest %s failed for %s: %s", action, case.dispute_id, exc)
        raise ContestError(
            f"Razorpay contest ({action}) failed: {exc} "
            "The case state is unchanged and the operation can be retried."
        ) from exc

    record_contest_attempt(
        settings.paths.case_db, case.dispute_id, action, True, actor, payload, response=response,
    )
    return response


def save_draft_to_razorpay(
    case: CaseRecord,
    evidence: CaseEvidence,
    package: EvidencePackage,
    investigation: InvestigationResult,
    *,
    actor: str,
    settings: Settings | None = None,
    output_dir: Path | None = None,
    client: RazorpayClient | None = None,
) -> dict[str, Any]:
    """PATCH .../contest with action="draft". Advances APPROVED -> DRAFTED.

    Saves evidence at Razorpay without submitting it to the bank. Reversible
    in the sense that nothing has been sent onward yet.
    """
    settings = settings or load_settings(require_razorpay=True)
    response = _send_contest(
        case, evidence, package, investigation, "draft", actor, settings, output_dir, client
    )
    if case.case_state == "APPROVED":
        transition_case_state(
            settings.paths.case_db, case.dispute_id, "DRAFTED", actor=actor,
            action="contest_draft_saved",
            reason="Contest draft saved at Razorpay (action=draft). Not submitted.",
        )
    return response


def submit_contest(
    case: CaseRecord,
    evidence: CaseEvidence,
    package: EvidencePackage,
    investigation: InvestigationResult,
    *,
    actor: str,
    human_confirmed: bool = False,
    settings: Settings | None = None,
    output_dir: Path | None = None,
    client: RazorpayClient | None = None,
) -> dict[str, Any]:
    """PATCH .../contest with action="submit". Advances DRAFTED -> SUBMITTED.

    `human_confirmed` has no default that permits submission: a caller must
    pass it explicitly. This is the last gate before evidence goes to the
    issuing bank, and it exists so that no amount of upstream automation can
    reach this line by itself.
    """
    if not human_confirmed:
        raise SubmissionBlocked(
            "submit_contest requires human_confirmed=True. A human must "
            "explicitly confirm submission - the AI never submits a dispute."
        )
    if not actor or not actor.strip():
        raise SubmissionBlocked("submit_contest requires the reviewer's identity.")

    settings = settings or load_settings(require_razorpay=True)
    response = _send_contest(
        case, evidence, package, investigation, "submit", actor, settings, output_dir, client
    )
    if case.case_state == "DRAFTED":
        transition_case_state(
            settings.paths.case_db, case.dispute_id, "SUBMITTED", actor=actor,
            action="contest_submitted",
            reason=f"Contest SUBMITTED to Razorpay by {actor} (action=submit).",
        )
    return response
