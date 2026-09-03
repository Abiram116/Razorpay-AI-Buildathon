"""The AI investigation agent.

One agent, one job: given the structured evidence bundle from
`merchant_db.get_case_evidence()` plus the Razorpay dispute facts, decide
whether the merchant can actually defend this chargeback - and say why, with
citations that resolve to real records.

Division of labour (spec section 9) is deliberate:
  * CODE loads the case, builds the evidence bundle, computes deadlines and
    amounts, validates the response, enforces citation integrity, drives the
    state machine, and persists results.
  * The MODEL does only the part that needs judgment: reading the narrative,
    weighing conflicting facts, and explaining the conclusion.

The model never submits anything. This module returns a recommendation; a
human decides (Phase 6/7).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .database import CaseRecord, get_case
from .investigation_schema import (
    CLASSIFICATIONS,
    INVESTIGATION_JSON_SCHEMA,
    InvestigationFailure,
    InvestigationResult,
    InvestigationValidationError,
    available_evidence_refs,
    validate_investigation_response,
)
from .merchant_db import CaseEvidence, get_case_evidence

logger = logging.getLogger(__name__)


class GroqUnavailable(RuntimeError):
    """The Groq API could not be reached or returned a PERMANENT error."""


class GroqTransientError(RuntimeError):
    """A retryable Groq failure: rate limit, timeout, connection, or 5xx.

    Kept distinct from GroqUnavailable because the correct response differs.
    A 429 that says "try again in 7.5s" is not a reason to send a case to
    manual review - it is a reason to wait 7.5 seconds. Only a genuinely
    permanent failure (bad key, bad request) should end the investigation.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# Groq's free tier enforces a tokens-per-minute budget; a full evaluation run
# WILL hit it (measured in Phase 4 - see BUILD_LOG). Parsed from the error so
# I wait exactly as long as the API asks, rather than guessing.
_RETRY_AFTER_PATTERN = re.compile(r"try again in ([0-9.]+)s")
MAX_TRANSIENT_RETRIES = 4
_DEFAULT_BACKOFF_SECONDS = 5.0


SYSTEM_PROMPT = """\
You are a chargeback investigation agent working for a MERCHANT. You decide \
whether the merchant has a defensible case against a payment dispute.

You are on the merchant's side, but you are useless to them if you are wrong. \
Recommending a contest the merchant cannot win costs them money and time; so \
does abandoning a case they could have won. Be accurate, not loyal.

CLASSIFICATION - describes the strength of THE MERCHANT'S DEFENCE, never the \
strength of the customer's claim:
  STRONG_CASE = the merchant's records affirmatively DISPROVE the customer's \
claim. Contest it.
  WEAK_CASE   = there is some supporting evidence, but with real gaps, or \
unresolved conflicts. Contesting is a judgment call.
  NO_CASE     = the merchant cannot show it met its obligation, or its own \
records SUPPORT the customer. Do not contest.

ABSOLUTE RULES:
1. Use ONLY the evidence provided in the EVIDENCE section. You have no other \
knowledge of this order.
2. NEVER invent evidence, tracking numbers, dates, messages, documents or \
policies. If something is not in the EVIDENCE section, it does not exist.
3. Absence of a record means "we have no record of this", NOT "this did not \
happen" and NOT "the merchant is hiding something". State it as missing.
4. Every claim in `reason` must be traceable to a citation in \
`supporting_evidence`.
5. Distinguish FACT (stated in the evidence) from INFERENCE (your reading of \
it). Word inferences as inferences.
6. If two pieces of evidence conflict, put the conflict in \
`conflicting_evidence` explicitly - do not silently pick a side.
7. When the evidence is insufficient, prefer WEAK_CASE or NO_CASE. Never \
inflate a classification to be helpful.
8. Consider whether the evidence actually ADDRESSES this specific dispute \
reason. Proof of delivery does not answer a "product not as described" claim.
9. A missing shipment record is EXPECTED and not suspicious when the product \
type is digital or a service. Judge it on proof of service/access instead.

CITATIONS: every entry in `supporting_evidence` must have a `reference` that \
is copied EXACTLY from the "Citable references" list given to you. Do not \
invent, reformat, or guess a reference. If you cannot cite it, do not claim it.

You are producing a RECOMMENDATION for a human reviewer. You never submit or \
contest anything yourself."""


def _fmt_money(amount_minor: int, currency: str) -> str:
    return f"{currency} {amount_minor / 100:,.2f}"


def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "not recorded"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_investigation_prompt(case: CaseRecord, evidence: CaseEvidence) -> str:
    """Render the case into the deterministic text the model sees.

    Everything here is drawn from the database; nothing is summarised or
    interpreted on the way in. Timestamps are rendered absolute so the model
    never has to do date arithmetic (it is bad at it and code is not).
    """
    order = evidence.order
    lines: list[str] = []

    lines.append("=== DISPUTE (reported by Razorpay) ===")
    lines.append(f"dispute_id: {case.dispute_id}")
    lines.append(f"payment_id: {case.payment_id}")
    lines.append(f"disputed_amount: {_fmt_money(case.amount, case.currency)}")
    lines.append(f"reason_code: {case.reason_code}")
    lines.append(f"dispute_status: {case.dispute_status}")
    lines.append(f"phase: {case.phase}")
    lines.append(f"respond_by: {_fmt_ts(case.respond_by)}")
    lines.append("")

    lines.append("=== MERCHANT ORDER RECORD ===")
    lines.append(f"merchant_order_id: {order.merchant_order_id}")
    lines.append(f"product: {order.product}")
    lines.append(f"product_type: {order.product_type}")
    lines.append(f"order_amount: {_fmt_money(order.amount, order.currency)}")
    lines.append(f"order_status: {order.order_status}")
    lines.append(f"ordered_at: {_fmt_ts(order.order_timestamp)}")
    lines.append(f"shipping_address: {order.shipping_address or 'not recorded'}")
    lines.append(f"billing_address: {order.billing_address or 'not recorded'}")
    if order.amount != case.amount:
        lines.append(
            f"NOTE: disputed amount differs from order amount "
            f"({_fmt_money(case.amount, case.currency)} vs {_fmt_money(order.amount, order.currency)})."
        )
    lines.append("")

    lines.append("=== SHIPMENT / DELIVERY ===")
    if evidence.shipment is None:
        lines.append(
            f"No shipment record exists for this order. "
            f"(product_type is '{order.product_type}'.)"
        )
    else:
        sh = evidence.shipment
        lines.append(f"delivery_status: {sh.delivery_status}")
        lines.append(f"tracking_id: {sh.tracking_id or 'not recorded'}")
        lines.append(f"courier: {sh.courier or 'not recorded'}")
        lines.append(f"shipped_at: {_fmt_ts(sh.shipped_at)}")
        lines.append(f"delivered_at: {_fmt_ts(sh.delivered_at)}")
        lines.append(f"delivery_location: {sh.delivery_location or 'not recorded'}")
        lines.append(f"recipient_confirmation: {sh.recipient_confirmation or 'NONE ON FILE'}")
    lines.append("")

    lines.append("=== CUSTOMER COMMUNICATIONS ===")
    if not evidence.communications:
        lines.append("No customer communications on file for this order.")
    else:
        for comm in evidence.communications:
            who = "CUSTOMER" if comm.direction == "inbound" else "MERCHANT"
            lines.append(
                f"[communication:{comm.id}] {_fmt_ts(comm.timestamp)} "
                f"via {comm.channel}, from {who}: \"{comm.message}\""
            )
    lines.append("")

    lines.append("=== REFUND HISTORY ===")
    if evidence.refund is None:
        lines.append("No refund record exists for this order.")
    else:
        rf = evidence.refund
        lines.append(f"refund_requested_by_customer: {rf.refund_requested}")
        lines.append(f"refund_status: {rf.refund_status}")
        lines.append(
            "refund_amount: "
            + (_fmt_money(rf.refund_amount, order.currency) if rf.refund_amount else "none")
        )
        lines.append(f"refund_processed_at: {_fmt_ts(rf.refund_timestamp)}")
        lines.append(f"refund_reason_on_file: {rf.reason or 'not recorded'}")
    lines.append("")

    lines.append("=== SUPPORTING DOCUMENTS ON FILE ===")
    if not evidence.documents:
        lines.append("No supporting documents on file for this order.")
    else:
        for doc in evidence.documents:
            lines.append(
                f"[document:{doc.id}] type={doc.document_type} file={doc.filename}: "
                f"{doc.description}"
            )
    lines.append("")

    lines.append("=== MERCHANT POLICIES (store-wide) ===")
    if not evidence.policies:
        lines.append("No policies on file.")
    else:
        for policy in evidence.policies:
            lines.append(f"[policy:{policy.policy_type}] (version {policy.version})")
            lines.append(policy.content)
            lines.append("")

    return "\n".join(lines)


def build_citation_block(allowed_refs: set[str]) -> str:
    refs = "\n".join(f"  {r}" for r in sorted(allowed_refs))
    return (
        "=== CITABLE REFERENCES ===\n"
        "These are the ONLY values permitted in supporting_evidence[].reference.\n"
        "Copy them exactly as written:\n"
        f"{refs}\n"
    )


def _call_groq(
    settings: Settings, system_prompt: str, user_prompt: str, model: str
) -> dict[str, Any]:
    """One Groq call with strict structured output. Raises GroqUnavailable."""
    try:
        from groq import Groq
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise GroqUnavailable("groq package is not installed") from exc

    if not settings.ai.api_key:
        raise GroqUnavailable(
            "GROQ_API_KEY is not set. Add it to .env - the investigation "
            "cannot run without it."
        )

    from groq import (
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
    )

    client = Groq(api_key=settings.ai.api_key, timeout=float(settings.ai.timeout_seconds))
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0,  # determinism matters for evaluation (Phase 8)
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "chargeback_investigation",
                    "schema": INVESTIGATION_JSON_SCHEMA,
                    "strict": True,
                },
            },
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    except RateLimitError as exc:
        match = _RETRY_AFTER_PATTERN.search(str(exc))
        wait = float(match.group(1)) if match else _DEFAULT_BACKOFF_SECONDS
        raise GroqTransientError(f"rate limited: {exc}", retry_after=wait) from exc
    except (APITimeoutError, APIConnectionError, InternalServerError) as exc:
        raise GroqTransientError(
            f"{type(exc).__name__}: {exc}", retry_after=_DEFAULT_BACKOFF_SECONDS
        ) from exc
    except Exception as exc:  # auth errors, bad requests - not worth retrying
        raise GroqUnavailable(f"{type(exc).__name__}: {exc}") from exc

    content = response.choices[0].message.content
    if not content:
        raise GroqUnavailable("Groq returned an empty response body")
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise InvestigationValidationError(
            f"model response was not valid JSON: {exc}"
        ) from exc


def _call_groq_with_backoff(
    settings: Settings, system_prompt: str, user_prompt: str
) -> dict[str, Any]:
    """Call Groq, waiting out transient failures rather than giving up.

    Rate limits and timeouts are waited on (up to MAX_TRANSIENT_RETRIES);
    permanent failures propagate immediately. If I exhaust the retries the
    transient failure is finally converted to GroqUnavailable, so the case
    still fails SAFE rather than hanging or being silently dropped.
    """
    last: GroqTransientError | None = None
    for attempt in range(MAX_TRANSIENT_RETRIES):
        try:
            return _call_groq(settings, system_prompt, user_prompt, settings.ai.model)
        except GroqTransientError as exc:
            last = exc
            wait = exc.retry_after or _DEFAULT_BACKOFF_SECONDS
            # Small margin: the API's own suggested delay is a lower bound.
            wait = min(wait + 0.5, 60.0)
            logger.warning(
                "transient Groq failure (attempt %d/%d), waiting %.1fs: %s",
                attempt + 1, MAX_TRANSIENT_RETRIES, wait, exc,
            )
            if attempt < MAX_TRANSIENT_RETRIES - 1:
                time.sleep(wait)
    raise GroqUnavailable(
        f"Groq still unavailable after {MAX_TRANSIENT_RETRIES} attempts: {last}"
    )


def investigate(
    case: CaseRecord,
    evidence: CaseEvidence,
    settings: Settings | None = None,
) -> InvestigationResult | InvestigationFailure:
    """Investigate one case. Pure with respect to storage - no DB writes.

    Retries once on a validation failure (feeding the error back to the
    model, per spec section 10), then fails safe. Never returns a fabricated
    investigation.
    """
    settings = settings or load_settings(require_razorpay=False)
    allowed_refs = available_evidence_refs(
        evidence, dispute_id=case.dispute_id, payment_id=case.payment_id
    )
    base_prompt = (
        build_investigation_prompt(case, evidence)
        + "\n"
        + build_citation_block(allowed_refs)
        + "\nInvestigate this dispute and return your structured finding."
    )

    attempts = 0
    max_attempts = 1 + max(0, settings.ai.max_retries)
    user_prompt = base_prompt
    last_error = ""

    while attempts < max_attempts:
        attempts += 1
        try:
            payload = _call_groq_with_backoff(settings, SYSTEM_PROMPT, user_prompt)
        except GroqUnavailable as exc:
            logger.error("investigation failed for %s: %s", case.dispute_id, exc)
            return InvestigationFailure(
                dispute_id=case.dispute_id,
                failure_reason="AI_UNAVAILABLE",
                detail=(
                    f"AI investigation unavailable - manual review required. {exc}"
                ),
                investigation_timestamp=int(time.time()),
                attempts=attempts,
            )
        except InvestigationValidationError as exc:
            last_error = str(exc)
            logger.warning(
                "invalid model response for %s (attempt %d): %s",
                case.dispute_id, attempts, last_error,
            )
            user_prompt = (
                base_prompt
                + f"\n\nYour previous response was REJECTED: {last_error}\n"
                "Return a corrected response that fixes exactly this problem."
            )
            continue

        try:
            return validate_investigation_response(
                payload,
                dispute_id=case.dispute_id,
                allowed_refs=allowed_refs,
                model=settings.ai.model,
                is_simulated_case=case.is_simulated,
            )
        except InvestigationValidationError as exc:
            last_error = str(exc)
            logger.warning(
                "model response failed validation for %s (attempt %d): %s",
                case.dispute_id, attempts, last_error,
            )
            user_prompt = (
                base_prompt
                + f"\n\nYour previous response was REJECTED: {last_error}\n"
                "Return a corrected response that fixes exactly this problem."
            )

    return InvestigationFailure(
        dispute_id=case.dispute_id,
        failure_reason="INVALID_AI_RESPONSE",
        detail=(
            "AI returned a response that failed validation after "
            f"{attempts} attempt(s) - manual review required. "
            f"Last error: {last_error}"
        ),
        investigation_timestamp=int(time.time()),
        attempts=attempts,
    )


def investigate_dispute(
    dispute_id: str,
    case_db: Path,
    merchant_db: Path,
    settings: Settings | None = None,
) -> InvestigationResult | InvestigationFailure:
    """Load a case by id, gather its evidence, and investigate it.

    Fails safe at every step: an unknown dispute, or a dispute with no
    merchant-side record at all, produces an InvestigationFailure - never a
    guess about what the order might have contained.
    """
    settings = settings or load_settings(require_razorpay=False)

    case = get_case(case_db, dispute_id)
    if case is None:
        return InvestigationFailure(
            dispute_id=dispute_id,
            failure_reason="CASE_NOT_FOUND",
            detail=f"No case with dispute_id={dispute_id!r} exists in the case database.",
            investigation_timestamp=int(time.time()),
            attempts=0,
        )

    evidence = get_case_evidence(merchant_db, payment_id=case.payment_id)
    if evidence is None:
        return InvestigationFailure(
            dispute_id=dispute_id,
            failure_reason="NO_MERCHANT_EVIDENCE",
            detail=(
                f"No merchant-side record exists for payment {case.payment_id}. "
                "Insufficient evidence - human review required."
            ),
            investigation_timestamp=int(time.time()),
            attempts=0,
        )

    return investigate(case, evidence, settings)
