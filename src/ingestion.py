"""The single ingestion entrypoint. Real webhook and simulator both call this.

This is the interface referenced throughout NOTES.md/README/BUILD_LOG: once a
DisputeEntity exists (parsed from a real webhook, or from the simulator), it
enters the system identically from here on. Nothing downstream needs to know
which source it came from except via the explicit `is_simulated` flag that
travels with the case forever.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import database
from .dispute_schema import DataSource, DisputeEntity, IngestedCase, PaymentSummary

logger = logging.getLogger(__name__)


def ingest_dispute(
    db_path: Path,
    dispute: DisputeEntity,
    payment: PaymentSummary | None,
    source: DataSource,
    *,
    actor: str = "system",
) -> database.CaseRecord:
    """Record a dispute event and return the resulting case row.

    `source` must be 'simulated' for anything not produced by a verified
    Razorpay webhook - callers must not launder simulated data through this
    function as 'razorpay_webhook'.
    """
    is_simulated = source == "simulated"
    case = IngestedCase(dispute=dispute, payment=payment, source=source, is_simulated=is_simulated)
    record = database.ingest_case(db_path, case, actor=actor)
    logger.info(
        "ingested dispute_id=%s source=%s state=%s amount=%s %s",
        dispute.id, source, record.case_state, dispute.amount, dispute.currency,
    )
    return record
