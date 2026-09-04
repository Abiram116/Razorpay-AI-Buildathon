"""Phase 10: the one-command entrypoint's decision logic.

Doesn't (and can't sensibly) test the os.execvp() launch itself - that
replaces the process, which is exactly the property that makes Ctrl-C stop
everything with nothing left running. What's tested is the part that can
go wrong silently: whether it correctly decides to seed or skip.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import run as run_entrypoint
from src.config import Paths


@pytest.fixture()
def fake_settings(tmp_path):
    from src.config import load_settings
    settings = load_settings(require_razorpay=False)
    object.__setattr__(settings, "paths", Paths(
        merchant_db=tmp_path / "merchant.db", case_db=tmp_path / "cases.db",
        generated_docs=tmp_path / "generated",
    ))
    return settings


def test_seeds_when_no_demo_data_exists(fake_settings, monkeypatch):
    monkeypatch.setattr(run_entrypoint.sys, "argv", ["run.py"])
    with patch.object(run_entrypoint, "load_settings", return_value=fake_settings), \
         patch.object(run_entrypoint.subprocess, "run") as mock_run, \
         patch.object(run_entrypoint.os, "execvp") as mock_exec:
        mock_run.return_value = MagicMock(returncode=0)
        run_entrypoint.main()

    mock_run.assert_called_once()
    seed_argv = mock_run.call_args[0][0]
    assert "seed_merchant_db.py" in seed_argv[-1] or "seed_merchant_db.py" in " ".join(seed_argv)
    assert "--reset" not in seed_argv
    mock_exec.assert_called_once()  # still launches the dashboard after seeding


def test_skips_seeding_when_demo_data_already_exists(fake_settings, monkeypatch):
    fake_settings.paths.case_db.touch()
    fake_settings.paths.merchant_db.touch()
    monkeypatch.setattr(run_entrypoint.sys, "argv", ["run.py"])
    with patch.object(run_entrypoint, "load_settings", return_value=fake_settings), \
         patch.object(run_entrypoint.subprocess, "run") as mock_run, \
         patch.object(run_entrypoint.os, "execvp") as mock_exec:
        run_entrypoint.main()

    mock_run.assert_not_called()  # no Groq quota burned for nothing
    mock_exec.assert_called_once()


def test_reset_flag_reseeds_even_if_data_exists(fake_settings, monkeypatch):
    fake_settings.paths.case_db.touch()
    fake_settings.paths.merchant_db.touch()
    monkeypatch.setattr(run_entrypoint.sys, "argv", ["run.py", "--reset"])
    with patch.object(run_entrypoint, "load_settings", return_value=fake_settings), \
         patch.object(run_entrypoint.subprocess, "run") as mock_run, \
         patch.object(run_entrypoint.os, "execvp") as mock_exec:
        mock_run.return_value = MagicMock(returncode=0)
        run_entrypoint.main()

    mock_run.assert_called_once()
    seed_argv = mock_run.call_args[0][0]
    assert "--reset" in seed_argv
    mock_exec.assert_called_once()


def test_failed_seeding_does_not_launch_the_dashboard(fake_settings, monkeypatch):
    """A broken seed must not silently open an empty/stale dashboard."""
    monkeypatch.setattr(run_entrypoint.sys, "argv", ["run.py"])
    with patch.object(run_entrypoint, "load_settings", return_value=fake_settings), \
         patch.object(run_entrypoint.subprocess, "run") as mock_run, \
         patch.object(run_entrypoint.os, "execvp") as mock_exec:
        mock_run.return_value = MagicMock(returncode=1)
        exit_code = run_entrypoint.main()

    assert exit_code != 0
    mock_exec.assert_not_called()
