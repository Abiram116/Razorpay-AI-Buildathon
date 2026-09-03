"""The investigation result contract, and the checks that make it trustworthy.

The central idea of this module: "the AI must not invent evidence" is not
enforceable by asking politely in a prompt. So every evidence citation the
model returns is a structured reference (`document:7`, `communication:12`,
`shipment:ORD-1001`) that I resolve against the evidence bundle I actually
handed it. A citation that doesn't resolve is a hallucination, and the
response is rejected - deterministically, in code, not by trusting the model.

Everything else here is ordinary validation: the classification must be one
of exactly three values, confidence must be in range, required fields must
be present and non-empty.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from .merchant_db import CaseEvidence

# The only three classifications. Defined explicitly (not as a bare enum) and
# described in the prompt too - Phase 1 taught me that an undefined enum gets
# a plausible-but-wrong interpretation (BUILD_LOG 2026-09-02-03).
CLASSIFICATIONS = ("STRONG_CASE", "WEAK_CASE", "NO_CASE")

RECOMMENDED_ACTIONS = ("CONTEST", "DO_NOT_CONTEST", "MANUAL_REVIEW")

# An evidence reference is "<kind>:<id>". Kinds map onto the tables in
# merchant_db plus the dispute/payment facts Razorpay supplies.
EVIDENCE_REF_PATTERN = re.compile(r"^(order|shipment|communication|refund|document|policy|dispute|payment):(.+)$")


class InvestigationValidationError(ValueError):
    """The model's response is structurally wrong, or cites evidence that
    does not exist in the bundle it was given."""


@dataclass(frozen=True)
class EvidenceCitation:
    """One resolved reference back to a real merchant/Razorpay record."""

    kind: str
    ref_id: str
    note: str

    @property
    def reference(self) -> str:
        return f"{self.kind}:{self.ref_id}"


@dataclass(frozen=True)
class InvestigationResult:
    dispute_id: str
    classification: str
    confidence: float
    executive_summary: str
    reason: str
    supporting_evidence: list[EvidenceCitation]
    missing_evidence: list[str]
    conflicting_evidence: list[str]
    recommended_action: str
    risk_factors: list[str]
    investigation_timestamp: int
    model: str
    is_simulated_case: bool

    @property
    def succeeded(self) -> bool:
        return True

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InvestigationResult":
        """Rebuild a stored result. The inverse of to_dict().

        Lives here rather than in each consumer so the CLI, the dashboard and
        anything later all rehydrate identically.
        """
        citations = []
        for item in payload.get("supporting_evidence", []):
            kind, _, ref_id = item["reference"].partition(":")
            citations.append(EvidenceCitation(kind=kind, ref_id=ref_id, note=item["note"]))
        return cls(
            dispute_id=payload["dispute_id"],
            classification=payload["classification"],
            confidence=payload["confidence"],
            executive_summary=payload["executive_summary"],
            reason=payload["reason"],
            supporting_evidence=citations,
            missing_evidence=list(payload.get("missing_evidence", [])),
            conflicting_evidence=list(payload.get("conflicting_evidence", [])),
            recommended_action=payload["recommended_action"],
            risk_factors=list(payload.get("risk_factors", [])),
            investigation_timestamp=payload["investigation_timestamp"],
            model=payload["model"],
            is_simulated_case=payload["is_simulated_case"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispute_id": self.dispute_id,
            "classification": self.classification,
            "confidence": self.confidence,
            "executive_summary": self.executive_summary,
            "reason": self.reason,
            "supporting_evidence": [
                {"reference": c.reference, "note": c.note} for c in self.supporting_evidence
            ],
            "missing_evidence": self.missing_evidence,
            "conflicting_evidence": self.conflicting_evidence,
            "recommended_action": self.recommended_action,
            "risk_factors": self.risk_factors,
            "investigation_timestamp": self.investigation_timestamp,
            "model": self.model,
            "is_simulated_case": self.is_simulated_case,
        }


@dataclass(frozen=True)
class InvestigationFailure:
    """A case I could NOT investigate.

    Deliberately a distinct type rather than an InvestigationResult with a
    low confidence: a failed investigation must never be mistakable for a
    real finding of "no case". Callers route this to human review.
    """

    dispute_id: str
    failure_reason: str
    detail: str
    investigation_timestamp: int
    attempts: int

    @property
    def succeeded(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispute_id": self.dispute_id,
            "failure_reason": self.failure_reason,
            "detail": self.detail,
            "investigation_timestamp": self.investigation_timestamp,
            "attempts": self.attempts,
        }


# The strict JSON schema handed to Groq. Every field required; no extras.
# Phase 1 measured that strict json_schema is the only mode that reliably
# produces my field names (NOTES.md N-010).
INVESTIGATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classification": {"type": "string", "enum": list(CLASSIFICATIONS)},
        "confidence": {"type": "number"},
        "executive_summary": {"type": "string"},
        "reason": {"type": "string"},
        "supporting_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reference": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["reference", "note"],
                "additionalProperties": False,
            },
        },
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
        "conflicting_evidence": {"type": "array", "items": {"type": "string"}},
        "recommended_action": {"type": "string", "enum": list(RECOMMENDED_ACTIONS)},
        "risk_factors": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "classification", "confidence", "executive_summary", "reason",
        "supporting_evidence", "missing_evidence", "conflicting_evidence",
        "recommended_action", "risk_factors",
    ],
    "additionalProperties": False,
}


def available_evidence_refs(evidence: CaseEvidence, *, dispute_id: str, payment_id: str) -> set[str]:
    """Every reference the model is allowed to cite for this case.

    This is the whitelist. Anything outside it is, by definition, invented.
    """
    refs = {
        f"order:{evidence.order.merchant_order_id}",
        f"dispute:{dispute_id}",
        f"payment:{payment_id}",
    }
    if evidence.shipment is not None:
        refs.add(f"shipment:{evidence.shipment.merchant_order_id}")
    if evidence.refund is not None:
        refs.add(f"refund:{evidence.refund.merchant_order_id}")
    for comm in evidence.communications:
        if comm.id is not None:
            refs.add(f"communication:{comm.id}")
    for doc in evidence.documents:
        if doc.id is not None:
            refs.add(f"document:{doc.id}")
    for policy in evidence.policies:
        refs.add(f"policy:{policy.policy_type}")
    return refs


def validate_investigation_response(
    payload: dict[str, Any],
    *,
    dispute_id: str,
    allowed_refs: set[str],
    model: str,
    is_simulated_case: bool,
) -> InvestigationResult:
    """Turn a raw model response into a validated InvestigationResult.

    Raises InvestigationValidationError on anything wrong - the caller
    retries once with the error text, then fails safe to human review.
    """
    classification = payload.get("classification")
    if classification not in CLASSIFICATIONS:
        raise InvestigationValidationError(
            f"classification must be one of {CLASSIFICATIONS}, got {classification!r}"
        )

    action = payload.get("recommended_action")
    if action not in RECOMMENDED_ACTIONS:
        raise InvestigationValidationError(
            f"recommended_action must be one of {RECOMMENDED_ACTIONS}, got {action!r}"
        )

    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise InvestigationValidationError(f"confidence must be a number, got {confidence!r}")
    if not 0.0 <= float(confidence) <= 1.0:
        raise InvestigationValidationError(
            f"confidence must be between 0.0 and 1.0, got {confidence}"
        )

    for text_field in ("executive_summary", "reason"):
        value = payload.get(text_field)
        if not isinstance(value, str) or not value.strip():
            raise InvestigationValidationError(f"{text_field} must be a non-empty string")

    for list_field in ("missing_evidence", "conflicting_evidence", "risk_factors"):
        value = payload.get(list_field)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise InvestigationValidationError(f"{list_field} must be a list of strings")

    raw_citations = payload.get("supporting_evidence")
    if not isinstance(raw_citations, list):
        raise InvestigationValidationError("supporting_evidence must be a list")

    citations: list[EvidenceCitation] = []
    for item in raw_citations:
        if not isinstance(item, dict):
            raise InvestigationValidationError("each supporting_evidence item must be an object")
        reference = item.get("reference")
        note = item.get("note")
        if not isinstance(reference, str) or not isinstance(note, str):
            raise InvestigationValidationError(
                "each supporting_evidence item needs string 'reference' and 'note'"
            )
        match = EVIDENCE_REF_PATTERN.match(reference.strip())
        if not match:
            raise InvestigationValidationError(
                f"evidence reference {reference!r} is not in '<kind>:<id>' form"
            )
        # THE anti-hallucination check: does this record actually exist in
        # what I gave the model?
        if reference.strip() not in allowed_refs:
            raise InvestigationValidationError(
                f"evidence reference {reference!r} does not exist in this case's evidence. "
                f"Cite only from: {sorted(allowed_refs)}"
            )
        citations.append(EvidenceCitation(kind=match.group(1), ref_id=match.group(2), note=note))

    # A case cannot be called defensible with nothing to point at.
    if classification == "STRONG_CASE" and not citations:
        raise InvestigationValidationError(
            "STRONG_CASE requires at least one supporting_evidence citation"
        )

    return InvestigationResult(
        dispute_id=dispute_id,
        classification=classification,
        confidence=float(confidence),
        executive_summary=payload["executive_summary"].strip(),
        reason=payload["reason"].strip(),
        supporting_evidence=citations,
        missing_evidence=list(payload["missing_evidence"]),
        conflicting_evidence=list(payload["conflicting_evidence"]),
        recommended_action=action,
        risk_factors=list(payload["risk_factors"]),
        investigation_timestamp=int(time.time()),
        model=model,
        is_simulated_case=is_simulated_case,
    )
