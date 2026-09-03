"""Central configuration.

Only four things are secrets and therefore live in .env:
    RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET, GROQ_API_KEY

Everything else is a tunable, not a secret, so it lives in the TUNABLES block
below where it can be read and reviewed alongside the code.

Nothing here ever prints or returns raw key material; `redact()` is the only
sanctioned way to show it to a human.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


# =====================================================================
# TUNABLES - not secrets, edit here
# =====================================================================

# Groq model used by the investigation agent.
#
# Chosen by measurement against this account's live model list, not the docs
# (the docs page still advertises llama-3.3-70b-versatile, which this account
# does not serve). Benchmark on a deliberately ambiguous "courier scanned to
# hub, no recipient proof" case, temperature 0, strict json_schema:
#
#   openai/gpt-oss-20b    schema valid 0/6   <- fails exactly on hard cases
#   openai/gpt-oss-120b   schema valid 6/6   avg 1.53s
#   qwen/qwen3.6-27b      cannot satisfy the schema at all
#
# 20b reasons correctly on clear-cut cases but drops required fields when the
# evidence is genuinely ambiguous - the cases that matter most here. 120b is
# still fast enough for an interactive dashboard.
AI_MODEL = "openai/gpt-oss-120b"

# Groq rejects a response that violates the schema, so the agent must be able
# to fall back. Used only if AI_MODEL is unavailable.
AI_MODEL_FALLBACK = "openai/gpt-oss-20b"

# Structured output mode. json_schema (strict) is required: plain json_object
# returns valid JSON that ignores my field names.
AI_RESPONSE_FORMAT = "json_schema"
AI_TIMEOUT_SECONDS = 60
AI_MAX_RETRIES = 1

# Deadline urgency, in hours remaining before the dispute's respond_by.
DEADLINE_CRITICAL_HOURS = 24
DEADLINE_WARNING_HOURS = 72

# Documented maximum length of the Razorpay dispute evidence `summary` field.
CONTEST_SUMMARY_MAX_CHARS = 1000

# Operational cost ASSUMPTIONS in INR - my own placeholders for the metrics
# module, NOT published Razorpay fees. The chargeback penalty is deliberately
# left unset: I haven't verified a figure from an authoritative source.
COST_MANUAL_REVIEW_INR = 250
COST_AI_INVESTIGATION_INR = 5
CHARGEBACK_PENALTY_FEE_INR: int | None = None

# Storage locations, relative to the project root.
MERCHANT_DB_PATH = "data/merchant/merchant.db"
CASE_DB_PATH = "data/merchant/cases.db"
GENERATED_DOCS_DIR = "data/generated"

# =====================================================================


class ConfigError(RuntimeError):
    """Raised when configuration is missing or unsafe."""


def redact(secret: str | None) -> str:
    """Render key material safe for logs and the UI."""
    if not secret:
        return "<not set>"
    if secret.startswith(("rzp_test_", "rzp_live_")):
        return f"{secret[:13]}{'*' * 8}"
    return f"{secret[:3]}{'*' * 8}" if len(secret) > 3 else "*" * 8


def _env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() or None if value else None


@dataclass(frozen=True)
class RazorpayConfig:
    key_id: str
    key_secret: str
    webhook_secret: str | None

    @property
    def is_test_mode(self) -> bool:
        return self.key_id.startswith("rzp_test_")

    @property
    def mode_label(self) -> str:
        if self.is_test_mode:
            return "TEST"
        if self.key_id.startswith("rzp_live_"):
            return "LIVE"
        return "UNKNOWN"

    def safe_summary(self) -> dict[str, str]:
        """Describe the credentials without revealing them."""
        return {
            "key_id": redact(self.key_id),
            "key_secret": redact(self.key_secret),
            "webhook_secret": redact(self.webhook_secret),
            "mode": self.mode_label,
        }


@dataclass(frozen=True)
class AIConfig:
    api_key: str | None
    model: str = AI_MODEL
    fallback_model: str = AI_MODEL_FALLBACK
    timeout_seconds: int = AI_TIMEOUT_SECONDS
    max_retries: int = AI_MAX_RETRIES


@dataclass(frozen=True)
class DeadlineConfig:
    critical_hours: int = DEADLINE_CRITICAL_HOURS
    warning_hours: int = DEADLINE_WARNING_HOURS


@dataclass(frozen=True)
class CostModelConfig:
    """Operational cost assumptions - see the TUNABLES block."""

    manual_review_inr: int = COST_MANUAL_REVIEW_INR
    ai_investigation_inr: int = COST_AI_INVESTIGATION_INR
    chargeback_penalty_fee_inr: int | None = CHARGEBACK_PENALTY_FEE_INR

    @property
    def penalty_is_assumed(self) -> bool:
        return self.chargeback_penalty_fee_inr is None


@dataclass(frozen=True)
class Paths:
    merchant_db: Path
    case_db: Path
    generated_docs: Path


@dataclass(frozen=True)
class Settings:
    razorpay: RazorpayConfig
    ai: AIConfig
    deadlines: DeadlineConfig
    costs: CostModelConfig
    paths: Paths
    contest_summary_max_chars: int = CONTEST_SUMMARY_MAX_CHARS


def load_settings(require_razorpay: bool = True) -> Settings:
    """Build Settings from .env plus the TUNABLES above."""
    key_id = _env("RAZORPAY_KEY_ID")
    key_secret = _env("RAZORPAY_KEY_SECRET")

    if require_razorpay and (not key_id or not key_secret):
        raise ConfigError(
            "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are not set.\n"
            "  1. cp .env.example .env\n"
            "  2. Razorpay Dashboard -> switch to Test Mode -> Settings -> "
            "API Keys -> Generate Test Key\n"
            "  3. Paste the key id (rzp_test_...) and secret into .env"
        )

    razorpay_cfg = RazorpayConfig(
        key_id=key_id or "",
        key_secret=key_secret or "",
        webhook_secret=_env("RAZORPAY_WEBHOOK_SECRET"),
    )

    # Hard safety rail: this prototype must never touch live merchant money.
    if razorpay_cfg.mode_label == "LIVE":
        raise ConfigError(
            "RAZORPAY_KEY_ID is a LIVE key (rzp_live_...). This prototype only "
            "runs against Test Mode. Use a rzp_test_ key."
        )

    return Settings(
        razorpay=razorpay_cfg,
        ai=AIConfig(api_key=_env("GROQ_API_KEY")),
        deadlines=DeadlineConfig(),
        costs=CostModelConfig(),
        paths=Paths(
            merchant_db=PROJECT_ROOT / MERCHANT_DB_PATH,
            case_db=PROJECT_ROOT / CASE_DB_PATH,
            generated_docs=PROJECT_ROOT / GENERATED_DOCS_DIR,
        ),
    )
