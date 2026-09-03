"""Phase 1 guardrail tests: secrets stay hidden, live keys stay blocked."""

import pytest

from src.config import ConfigError, RazorpayConfig, load_settings, redact


def test_redact_never_leaks_the_secret_tail():
    secret = "averysecretvalue12345"
    assert secret not in redact(secret)
    assert redact(secret).startswith("ave")


def test_redact_shows_test_prefix_but_not_the_key():
    out = redact("rzp_test_abcdefghijklmn")
    assert out.startswith("rzp_test_")
    assert "ijklmn" not in out


def test_redact_handles_unset():
    assert redact(None) == "<not set>"
    assert redact("") == "<not set>"


def test_safe_summary_contains_no_raw_material():
    cfg = RazorpayConfig(
        key_id="rzp_test_abcdefghijklmn",
        key_secret="topsecretsecret",
        webhook_secret="hooksecret",
    )
    blob = str(cfg.safe_summary())
    assert "topsecretsecret" not in blob
    assert "hooksecret" not in blob
    assert cfg.is_test_mode and cfg.mode_label == "TEST"


def test_live_key_is_always_refused(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_abcdefghijklmn")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "x")
    with pytest.raises(ConfigError, match="LIVE key"):
        load_settings()


def test_missing_credentials_give_actionable_error(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    with pytest.raises(ConfigError, match="Test Mode"):
        load_settings(require_razorpay=True)


def test_groq_key_is_read_from_env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_testvalue")
    assert load_settings(require_razorpay=False).ai.api_key == "gsk_testvalue"


def test_penalty_fee_is_unset_by_default():
    costs = load_settings(require_razorpay=False).costs
    assert costs.chargeback_penalty_fee_inr is None
    assert costs.penalty_is_assumed


def test_contest_summary_limit_matches_razorpay_docs():
    assert load_settings(require_razorpay=False).contest_summary_max_chars == 1000
