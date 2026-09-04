"""Persistence for evaluation runs - the thing that makes a run resumable.

A full 200-case run takes over an hour on Groq's free tier, purely because
of the token budget. Anything that takes an hour will get interrupted:
Ctrl-C, a dropped connection, a laptop lid. So every case result is committed
the moment it completes, and a resumed run skips whatever is already stored.

Nothing here re-runs a case that already has a result. That's both what makes
an interrupted run recoverable and what stops a re-run burning rate-limit
budget on work already done.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class StoredResult:
    case_id: str
    ground_truth: str
    split: str
    archetype: str
    succeeded: bool
    classification: str | None
    confidence: float | None
    recommended_action: str | None
    failure_reason: str | None
    amount: int
    latency_ms: int
    created_at: int


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_evaluation_db(db_path: Path) -> None:
    with connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS eval_runs (
                run_id           TEXT PRIMARY KEY,
                dataset_version  TEXT NOT NULL,
                split            TEXT NOT NULL,
                model            TEXT NOT NULL,
                total_cases      INTEGER NOT NULL,
                started_at       INTEGER NOT NULL,
                updated_at       INTEGER NOT NULL,
                completed_at     INTEGER,
                status           TEXT NOT NULL
                                 CHECK (status IN ('running','completed','interrupted')),
                config_json      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS eval_results (
                run_id             TEXT NOT NULL,
                case_id            TEXT NOT NULL,
                ground_truth       TEXT NOT NULL,
                split              TEXT NOT NULL,
                archetype          TEXT NOT NULL,
                amount             INTEGER NOT NULL,
                succeeded          INTEGER NOT NULL CHECK (succeeded IN (0,1)),
                classification     TEXT,
                confidence         REAL,
                recommended_action TEXT,
                failure_reason     TEXT,
                latency_ms         INTEGER NOT NULL,
                result_json        TEXT NOT NULL,
                created_at         INTEGER NOT NULL,
                PRIMARY KEY (run_id, case_id),
                FOREIGN KEY (run_id) REFERENCES eval_runs(run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id);

            -- Final computed metrics, kept so a report can be regenerated
            -- without re-running (or re-deriving) anything.
            CREATE TABLE IF NOT EXISTS eval_metrics (
                run_id       TEXT PRIMARY KEY,
                computed_at  INTEGER NOT NULL,
                metrics_json TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES eval_runs(run_id)
            );
            """
        )


def make_run_id(dataset_version: str, split: str, model: str, fingerprint: str) -> str:
    """Deterministic, so `--resume` needs no bookkeeping from the operator.

    The same dataset + split + model is the same run, and picks up where it
    stopped. Changing any of them is a different experiment and gets its own
    run - which is the correct behaviour: results from two different models,
    or from two different versions of the dataset, must never land in one
    confusion matrix. `fingerprint` is a content hash of the actual cases, so
    editing the generator invalidates the run automatically rather than
    silently appending new results to old ones.
    """
    digest = hashlib.sha256(
        f"{dataset_version}|{split}|{model}|{fingerprint}".encode()
    ).hexdigest()[:10]
    return f"run_{dataset_version}_{split}_{digest}"


def start_or_resume_run(
    db_path: Path, run_id: str, dataset_version: str, split: str,
    model: str, total_cases: int, config: dict,
) -> bool:
    """Create the run if new. Returns True if this is a resume."""
    now = int(time.time())
    with connection(db_path) as conn:
        existing = conn.execute(
            "SELECT run_id FROM eval_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE eval_runs SET status='running', updated_at=? WHERE run_id=?",
                (now, run_id),
            )
            return True
        conn.execute(
            "INSERT INTO eval_runs (run_id, dataset_version, split, model, total_cases, "
            "started_at, updated_at, completed_at, status, config_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'running', ?)",
            (run_id, dataset_version, split, model, total_cases, now, now,
             json.dumps(config)),
        )
        return False


def completed_case_ids(db_path: Path, run_id: str) -> set[str]:
    """Cases already done for this run - skipped on resume."""
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT case_id FROM eval_results WHERE run_id = ?", (run_id,)
        ).fetchall()
        return {r["case_id"] for r in rows}


def record_result(
    db_path: Path, run_id: str, case_id: str, ground_truth: str, split: str,
    archetype: str, amount: int, result, latency_ms: int,
) -> None:
    """Persist one case immediately. This is the checkpoint - an interrupt
    after this line loses nothing."""
    payload = result.to_dict()
    now = int(time.time())
    with connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO eval_results (run_id, case_id, ground_truth, split, "
            "archetype, amount, succeeded, classification, confidence, recommended_action, "
            "failure_reason, latency_ms, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, case_id, ground_truth, split, archetype, amount,
             int(result.succeeded), payload.get("classification"),
             payload.get("confidence"), payload.get("recommended_action"),
             payload.get("failure_reason"), latency_ms, json.dumps(payload), now),
        )
        conn.execute("UPDATE eval_runs SET updated_at=? WHERE run_id=?", (now, run_id))


def mark_run(db_path: Path, run_id: str, status: str) -> None:
    now = int(time.time())
    with connection(db_path) as conn:
        conn.execute(
            "UPDATE eval_runs SET status=?, updated_at=?, "
            "completed_at=CASE WHEN ?='completed' THEN ? ELSE completed_at END "
            "WHERE run_id=?",
            (status, now, status, now, run_id),
        )


def load_results(db_path: Path, run_id: str) -> list[StoredResult]:
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM eval_results WHERE run_id = ? ORDER BY case_id", (run_id,)
        ).fetchall()
        return [
            StoredResult(
                case_id=r["case_id"], ground_truth=r["ground_truth"], split=r["split"],
                archetype=r["archetype"], succeeded=bool(r["succeeded"]),
                classification=r["classification"], confidence=r["confidence"],
                recommended_action=r["recommended_action"],
                failure_reason=r["failure_reason"], amount=r["amount"],
                latency_ms=r["latency_ms"], created_at=r["created_at"],
            )
            for r in rows
        ]


def get_run(db_path: Path, run_id: str) -> dict | None:
    with connection(db_path) as conn:
        row = conn.execute("SELECT * FROM eval_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["config"] = json.loads(record.pop("config_json"))
        return record


def list_runs(db_path: Path) -> list[dict]:
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM eval_runs ORDER BY started_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def save_metrics(db_path: Path, run_id: str, metrics: dict) -> None:
    with connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO eval_metrics (run_id, computed_at, metrics_json) "
            "VALUES (?, ?, ?)",
            (run_id, int(time.time()), json.dumps(metrics, indent=2)),
        )


def load_metrics(db_path: Path, run_id: str) -> dict | None:
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT metrics_json FROM eval_metrics WHERE run_id = ?", (run_id,)
        ).fetchone()
        return json.loads(row["metrics_json"]) if row else None
