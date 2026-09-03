"""Thin, defensive wrapper around the official Razorpay Python SDK.

Design rules for this module:
  * Every outbound call is wrapped so that a network/auth failure surfaces as
    a typed error the rest of the app can route to "manual review" instead of
    crashing (see RULE 10, fail safely).
  * Nothing here fabricates a Razorpay response. If the API cannot be reached
    the caller is told so; it never receives invented data.
  * Secrets are never logged. Only `settings.razorpay.safe_summary()` is
    ever emitted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import razorpay
from razorpay.errors import (
    BadRequestError,
    GatewayError,
    ServerError,
    SignatureVerificationError,
)
from requests.exceptions import RequestException

from .config import ConfigError, Settings, load_settings

logger = logging.getLogger(__name__)

# Razorpay entity ids are a prefix plus exactly 14 alphanumeric characters
# (verified: order_TX6aeI9VxWxNPK, disp_AHfqOvkldwsbqt). The API gateway does
# not route a malformed id at all -- it answers 404 with a bare
# {"message": "no Route matched with those values"} and no error.code, which
# the SDK cannot classify (see _call). Validating up front turns that into a
# clear local error instead of a misleading "service unavailable".
_ID_PATTERN = re.compile(r"^(order|pay|disp|doc)_[A-Za-z0-9]{14}$")

# Transient = worth retrying. NOTE: ServerError is deliberately NOT in this
# tuple. The SDK raises ServerError from a catch-all branch whenever the
# response body carries no recognised error.code, so it conflates genuine 5xx
# faults with unroutable requests. It is handled explicitly in _call.
_TRANSIENT_ERRORS = (GatewayError, RequestException)


def validate_entity_id(entity_id: str, prefix: str) -> None:
    """Reject a malformed Razorpay id before it reaches the network."""
    if not isinstance(entity_id, str) or not entity_id:
        raise RazorpayRequestError(f"Expected a {prefix}_... id, got {entity_id!r}.")
    if not _ID_PATTERN.match(entity_id) or not entity_id.startswith(f"{prefix}_"):
        raise RazorpayRequestError(
            f"{entity_id!r} is not a valid Razorpay {prefix} id "
            f"(expected {prefix}_ followed by 14 alphanumeric characters)."
        )


class RazorpayUnavailable(RuntimeError):
    """Razorpay could not be reached, or returned a server-side failure."""


class RazorpayAuthError(RuntimeError):
    """Credentials were rejected by Razorpay."""


class RazorpayRequestError(RuntimeError):
    """Razorpay rejected the request itself (bad id, bad payload)."""


@dataclass
class ConnectivityResult:
    """Outcome of the Phase 1 authentication probe."""

    authenticated: bool
    mode: str
    key_id_redacted: str
    detail: str
    endpoint_probed: str


class RazorpayClient:
    """Authenticated Razorpay client scoped to the operations I actually need."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings(require_razorpay=True)
        cfg = self.settings.razorpay
        if not cfg.key_id or not cfg.key_secret:
            raise ConfigError("Razorpay credentials are not configured.")
        self._client = razorpay.Client(auth=(cfg.key_id, cfg.key_secret))
        self._client.set_app_details(
            {"title": "ai-chargeback-defense-agent", "version": "0.1.0"}
        )

    # ------------------------------------------------------------------
    # error translation
    # ------------------------------------------------------------------
    def _call(self, description: str, fn, *args, **kwargs) -> Any:
        """Run an SDK call, converting SDK/network errors into my own types."""
        try:
            return fn(*args, **kwargs)
        except BadRequestError as exc:
            message = str(exc)
            # Razorpay returns 401 with this description for bad credentials.
            if "authentication" in message.lower() or "api key" in message.lower():
                logger.error("Razorpay authentication rejected during %s", description)
                raise RazorpayAuthError(
                    "Razorpay rejected the API credentials. Check "
                    "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET in .env."
                ) from exc
            logger.warning("Razorpay rejected request during %s: %s", description, message)
            raise RazorpayRequestError(f"{description} failed: {message}") from exc
        except ServerError as exc:
            # The SDK funnels both real 5xx faults and unclassifiable responses
            # (e.g. a gateway 404 with no error.code) into ServerError. An empty
            # message means I genuinely can't tell which, so say so rather
            # than promising a retry will help.
            detail = str(exc).strip()
            if not detail:
                logger.error(
                    "Razorpay returned an unclassifiable error during %s", description
                )
                raise RazorpayUnavailable(
                    f"Razorpay returned an unrecognised error while attempting "
                    f"{description}. This usually means the resource does not "
                    "exist, or Razorpay is degraded. Route this case to manual "
                    "review rather than assuming either."
                ) from exc
            logger.error("Razorpay server error during %s: %s", description, detail)
            raise RazorpayUnavailable(
                f"Razorpay reported a server error while attempting {description}: "
                f"{detail}. Retry, or route this case to manual review."
            ) from exc
        except _TRANSIENT_ERRORS as exc:
            logger.error("Razorpay unavailable during %s: %s", description, exc)
            raise RazorpayUnavailable(
                f"Unable to reach Razorpay while attempting {description}. "
                "Retry, or route this case to manual review."
            ) from exc

    # ------------------------------------------------------------------
    # connectivity
    # ------------------------------------------------------------------
    def verify_credentials(self) -> ConnectivityResult:
        """Prove the credentials work using a harmless read-only call.

        Uses `GET /v1/payments?count=1`: it creates nothing, moves no money,
        and returns 401 for bad credentials.
        """
        cfg = self.settings.razorpay
        endpoint = "GET /v1/payments?count=1"
        try:
            self._call("credential verification", self._client.payment.all, {"count": 1})
        except RazorpayAuthError as exc:
            return ConnectivityResult(False, cfg.mode_label, cfg.safe_summary()["key_id"], str(exc), endpoint)
        except (RazorpayUnavailable, RazorpayRequestError) as exc:
            return ConnectivityResult(False, cfg.mode_label, cfg.safe_summary()["key_id"], str(exc), endpoint)
        return ConnectivityResult(
            authenticated=True,
            mode=cfg.mode_label,
            key_id_redacted=str(cfg.safe_summary()["key_id"]),
            detail="Credentials accepted by Razorpay.",
            endpoint_probed=endpoint,
        )

    # ------------------------------------------------------------------
    # orders / payments
    # ------------------------------------------------------------------
    def create_order(
        self,
        amount_paise: int,
        currency: str = "INR",
        receipt: str | None = None,
        notes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create a real Razorpay (test-mode) order. Amount is in paise."""
        payload: dict[str, Any] = {"amount": amount_paise, "currency": currency}
        if receipt:
            payload["receipt"] = receipt
        if notes:
            payload["notes"] = notes
        return self._call("order creation", self._client.order.create, payload)

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        validate_entity_id(order_id, "order")
        return self._call(f"fetch order {order_id}", self._client.order.fetch, order_id)

    def list_orders(self, count: int = 10) -> dict[str, Any]:
        return self._call("list orders", self._client.order.all, {"count": count})

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        validate_entity_id(payment_id, "pay")
        return self._call(f"fetch payment {payment_id}", self._client.payment.fetch, payment_id)

    def list_payments(self, count: int = 10) -> dict[str, Any]:
        return self._call("list payments", self._client.payment.all, {"count": count})

    def order_payments(self, order_id: str) -> dict[str, Any]:
        validate_entity_id(order_id, "order")
        return self._call(
            f"fetch payments for order {order_id}", self._client.order.payments, order_id
        )

    # ------------------------------------------------------------------
    # disputes
    #
    # NOTE: the Razorpay API exposes no "create dispute" endpoint. Disputes
    # originate from the issuing bank or the customer. There is deliberately
    # no create_dispute() here -- see NOTES.md.
    # ------------------------------------------------------------------
    def list_disputes(self, count: int = 10) -> dict[str, Any]:
        return self._call("list disputes", self._client.dispute.all, {"count": count})

    def fetch_dispute(self, dispute_id: str) -> dict[str, Any]:
        validate_entity_id(dispute_id, "disp")
        return self._call(
            f"fetch dispute {dispute_id}", self._client.dispute.fetch, dispute_id
        )

    def contest_dispute(self, dispute_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """PATCH /v1/disputes/{id}/contest.

        `payload["action"]` must be "draft" or "submit". Submission is only
        ever reached after an explicit human confirmation upstream; this
        method does not decide that on its own.
        """
        validate_entity_id(dispute_id, "disp")
        action = payload.get("action")
        if action not in {"draft", "submit"}:
            raise ValueError("contest payload requires action='draft' or 'submit'")
        return self._call(
            f"contest dispute {dispute_id} (action={action})",
            self._client.dispute.contest,
            dispute_id,
            payload,
        )

    def upload_evidence_document(self, file_path: str, mime_type: str) -> dict[str, Any]:
        """POST /v1/documents with purpose=dispute_evidence."""
        with open(file_path, "rb") as handle:
            payload = {
                "file": (file_path.split("/")[-1], handle, mime_type),
                "purpose": "dispute_evidence",
            }
            return self._call(
                "evidence document upload", self._client.document.create, payload
            )

    # ------------------------------------------------------------------
    # webhooks
    # ------------------------------------------------------------------
    def verify_webhook_signature(self, raw_body: str, signature: str) -> bool:
        """Verify X-Razorpay-Signature over the RAW request body."""
        secret = self.settings.razorpay.webhook_secret
        if not secret:
            raise ConfigError("RAZORPAY_WEBHOOK_SECRET is not set.")
        try:
            self._client.utility.verify_webhook_signature(raw_body, signature, secret)
            return True
        except SignatureVerificationError:
            logger.warning("Rejected webhook with invalid signature.")
            return False


SAFE_PAYMENT_FIELDS = (
    "id", "entity", "amount", "currency", "status", "order_id", "method",
    "captured", "international", "amount_refunded", "refund_status",
    "description", "created_at", "notes",
)
SAFE_ORDER_FIELDS = (
    "id", "entity", "amount", "amount_paid", "amount_due", "currency",
    "receipt", "status", "attempts", "notes", "created_at",
)
SAFE_DISPUTE_FIELDS = (
    "id", "entity", "payment_id", "amount", "currency", "amount_deducted",
    "reason_code", "reason_description", "respond_by", "status", "phase",
    "created_at", "submitted_at",
)


def project(entity: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Pick only known-safe, non-PII fields from a Razorpay entity for logs."""
    return {k: entity.get(k) for k in fields if k in entity}
