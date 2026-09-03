"""AI Chargeback Defense Manager - the review dashboard.

Run:  uv run streamlit run dashboard/app.py

I kept this file dumb on purpose - it's just a view over everything I built
in src/, reading state and rendering it and passing your clicks back down.
Nothing in here decides anything on its own. If you take one thing away
from reading this file: the AI hands you a recommendation, and only you
(or whoever's reviewing) can actually act on it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ConfigError, load_settings  # noqa: E402
from src.database import (  # noqa: E402
    get_case,
    get_latest_investigation,
    list_cases,
    save_investigation,
)
from src.contest_service import (  # noqa: E402
    ContestError,
    SubmissionBlocked,
    build_local_draft,
    save_draft_to_razorpay,
    submit_contest,
)
from src.database import get_contest_attempts  # noqa: E402
from src.evidence_builder import EvidenceBuildError, build_evidence_package  # noqa: E402
from src.investigation_agent import investigate_dispute  # noqa: E402
from src.investigation_schema import InvestigationResult  # noqa: E402
from src.merchant_db import get_case_evidence  # noqa: E402
from src.review_workflow import (  # noqa: E402
    CASE_STATE_LABELS,
    advance_to_review,
    build_case_summary,
    case_state_label,
    deadline_status,
    reason_code_label,
    record_human_decision,
    review_history,
    summarise_queue,
    workflow_progress,
)

st.set_page_config(
    page_title="AI Chargeback Defense Manager",
    page_icon="⚖️",
    layout="wide",
)

CLASSIFICATION_STYLE = {
    "STRONG_CASE": ("🟢", "#16a34a", "Evidence supports contesting this dispute."),
    "WEAK_CASE": ("🟡", "#ca8a04", "Partial evidence. Contesting is a judgment call."),
    "NO_CASE": ("🔴", "#dc2626", "Evidence does not support contesting. Do not contest."),
}

URGENCY_COLOR = {
    "EXPIRED": "#dc2626", "CRITICAL": "#dc2626",
    "WARNING": "#ca8a04", "NORMAL": "#64748b",
}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def fmt_money(minor: int, currency: str) -> str:
    return f"{currency} {minor / 100:,.2f}"


def fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "not recorded"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def get_settings():
    """Deliberately NOT @st.cache_resource.

    load_settings() is cheap (reads .env, builds frozen dataclasses), and
    caching it holds a stale Settings for the life of the process - the app
    would then ignore a config change until a full restart, and it silently
    breaks test isolation between AppTest runs.
    """
    return load_settings(require_razorpay=False)


def provenance_badge(is_simulated: bool) -> str:
    if is_simulated:
        return (
            "<span style='background:#b45309;color:white;padding:2px 8px;"
            "border-radius:4px;font-size:0.75rem;font-weight:600'>"
            "SIMULATED TEST DISPUTE</span>"
        )
    return (
        "<span style='background:#0369a1;color:white;padding:2px 8px;"
        "border-radius:4px;font-size:0.75rem;font-weight:600'>"
        "RAZORPAY WEBHOOK</span>"
    )


def load_investigation(case_db: Path, dispute_id: str):
    stored = get_latest_investigation(case_db, dispute_id)
    if stored is None:
        return None, None
    if not stored["succeeded"]:
        return None, stored["result"]
    return InvestigationResult.from_dict(stored["result"]), None


# ----------------------------------------------------------------------
# sections
# ----------------------------------------------------------------------

def render_header() -> None:
    st.title("⚖️  AI Chargeback Defense Manager")
    st.markdown(
        "Investigates payment disputes against the merchant's own records, "
        "assembles the supporting evidence, and prepares a contest draft — "
        "**while every decision and submission stays with a human reviewer.**"
    )


def render_integration_proof(settings) -> None:
    """Quick honesty check for anyone reviewing this: what's actually live
    against Razorpay vs. what I've only been able to test against a mock.

    Short version - Razorpay doesn't give ANYONE a way to create a dispute
    in test mode, not even them internally as far as I can tell. A dispute
    only exists once a real bank has disputed a real charge on a real
    payment, so I can't fake my way into one just to demo the last step. I
    dug into this properly in Phase 1 before assuming it - see NOTES.md N-003
    if you want the receipts. Document upload turned out to have no such
    restriction (it just needs valid keys, no dispute required), so that one
    I could actually wire up live below instead of just asserting it works.
    """
    with st.expander("🔍 What's actually live here vs. what I could only test against a mock"):
        st.markdown(
            "**Genuinely live, not simulated:**\n"
            "- Order creation and credential check against Razorpay Test Mode\n"
            "- Webhook signature verification (HMAC-SHA256), tested over a real public URL\n"
            "- Evidence document upload (`POST /v1/documents`) — try it below, right now\n\n"
            "**The one thing I couldn't demo live, and why:**\n"
            "Razorpay just doesn't let anyone spin up a test dispute — I checked, there's "
            "no API for it, no special test card, nothing in the dashboard either. A "
            "dispute only exists because a real bank disputed a real charge on a real "
            "payment, so short of going live with actual money (which I'm not doing for "
            "a prototype), I can never fire the final `PATCH /v1/disputes/{id}/contest` "
            "call for real. You'll see the *Submission blocked* message on every case for "
            "exactly this reason. What I could do instead: make sure every field that call "
            "would send is real and correct — built by the same code path, checked against "
            "Razorpay's documented evidence categories, and pinned down by 26 tests against "
            "a mocked client (`tests/test_contest_service.py`) so I'm not just taking my own "
            "word for it."
        )
        st.markdown("---")
        st.markdown("**Don't take my word for it — this button makes a real call to `api.razorpay.com`:**")

        if st.button("🔐 Upload a real evidence document to Razorpay now", key="live_upload_proof"):
            with st.spinner("POST https://api.razorpay.com/v1/documents ..."):
                try:
                    from src.razorpay_client import RazorpayClient
                    from reportlab.platypus import Paragraph, SimpleDocTemplate
                    import tempfile

                    real_settings = load_settings(require_razorpay=True)
                    client = RazorpayClient(real_settings)
                    proof_path = Path(tempfile.mktemp(suffix=".pdf"))
                    SimpleDocTemplate(str(proof_path)).build([Paragraph(
                        "Live evidence upload proof - AI Chargeback Defense Manager - "
                        f"{datetime.now(tz=timezone.utc).isoformat()}",
                        _styles_for_proof(),
                    )])
                    response = client.upload_evidence_document(str(proof_path), "application/pdf")
                except Exception as exc:  # noqa: BLE001 - shown to the reviewer, not swallowed
                    st.error(f"Upload failed: {exc}")
                else:
                    st.success(
                        f"That's a real id, straight from Razorpay: `{response.get('id')}`"
                    )
                    st.json(response)
                    st.caption(
                        "purpose=dispute_evidence, exactly as their Documents API docs "
                        "say. No dispute needed for this call, which is the whole reason "
                        "I can show it to you live instead of just claiming it works."
                    )


def _styles_for_proof():
    from src.document_generator import _styles
    return _styles()["body"]


def render_overview(summaries) -> None:
    stats = summarise_queue(summaries)
    st.subheader("Overview")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total disputes", stats.total)
    c2.metric("Awaiting human review", stats.awaiting_review)
    c3.metric("Disputed amount", stats.amount_display)
    c4.metric(
        "Approaching deadline", stats.approaching_deadline,
        delta=f"{stats.expired} expired" if stats.expired else None,
        delta_color="inverse",
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🟢 Strong case", stats.strong)
    c2.metric("🟡 Weak case", stats.weak)
    c3.metric("🔴 No case", stats.no_case)
    c4.metric(
        "Not yet investigated", stats.not_investigated,
        delta=f"{stats.failed_investigations} failed" if stats.failed_investigations else None,
        delta_color="inverse",
    )


def render_queue(summaries) -> str | None:
    st.subheader("Dispute queue")
    if not summaries:
        st.info(
            "No disputes loaded. Use **Load demo cases** in the sidebar to seed "
            "the synthetic demo scenarios."
        )
        return None

    rows = []
    for s in summaries:
        case = s.case
        rows.append({
            "Dispute ID": case.dispute_id,
            "Payment": case.payment_id,
            "Amount": fmt_money(case.amount, case.currency),
            "Reason": reason_code_label(case.reason_code),
            "Dispute status": case.dispute_status,
            "AI": s.classification or ("FAILED" if s.investigation_failed else "—"),
            "Confidence": f"{s.confidence:.0%}" if s.confidence is not None else "—",
            "Deadline": s.deadline.label,
            "Review status": case_state_label(case.case_state),
            "Source": "SIMULATED" if case.is_simulated else "RAZORPAY",
        })
    st.dataframe(rows, width='stretch', hide_index=True)
    st.caption(
        "**Reason** is the customer's claim in plain language. **AI** is a "
        "recommendation, never a decision — see the case page for the full "
        "reasoning before acting on it."
    )

    options = [s.case.dispute_id for s in summaries]
    labels = {
        s.case.dispute_id: (
            f"{s.case.dispute_id}  ·  {fmt_money(s.case.amount, s.case.currency)}  ·  "
            f"{s.classification or 'not investigated'}"
        )
        for s in summaries
    }
    return st.selectbox(
        "Select a dispute to investigate",
        options, format_func=lambda d: labels[d], key="selected_dispute",
    )


def render_workflow_stepper(case_state: str) -> None:
    """The persistent "where is this case right now" anchor.

    Working a queue of cases in different states is exactly where a reviewer
    loses track — this renders the same seven-step strip on every case page
    so "what stage am I at" never requires re-reading the audit log.
    """
    steps = workflow_progress(case_state)
    cols = st.columns(len(steps))
    for col, step in zip(cols, steps):
        if step.is_stopped:
            icon, color = "⛔", "#94a3b8"
        elif step.is_current:
            icon, color = "🔵", "#2563eb"
        elif step.is_complete:
            icon, color = "✅", "#16a34a"
        else:
            icon, color = "⚪", "#94a3b8"
        weight = "700" if step.is_current else "400"
        col.markdown(
            f"<div style='text-align:center'>"
            f"<div style='font-size:1.1rem'>{icon}</div>"
            f"<div style='font-size:0.72rem;color:{color};font-weight:{weight}'>"
            f"{step.label}</div></div>",
            unsafe_allow_html=True,
        )
    if case_state == "OVERRULED":
        st.caption("⛔ This case was **rejected by a reviewer** at the review step and will not proceed further.")


def render_case_facts(case, evidence, settings) -> None:
    deadline = deadline_status(case.respond_by, settings)

    render_workflow_stepper(case.case_state)
    st.caption(f"Current status: **{case_state_label(case.case_state)}**")

    st.markdown(provenance_badge(case.is_simulated), unsafe_allow_html=True)
    if case.is_simulated:
        st.caption(
            "I generated this one myself for the demo — Razorpay doesn't give you "
            "any way to create a test dispute, so I built it to match their "
            "documented dispute schema exactly instead of faking something looser."
        )

    st.markdown(f"**Customer's claim:** {reason_code_label(case.reason_code)}")

    c1, c2, c3 = st.columns(3)
    c1.metric("Disputed amount", fmt_money(case.amount, case.currency))
    c2.metric("Dispute status", f"{case.dispute_status} / {case.phase}")
    c3.markdown(
        f"**Respond by**<br>{fmt_ts(case.respond_by)}<br>"
        f"<span style='color:{URGENCY_COLOR[deadline.urgency]};font-weight:600'>"
        f"{deadline.label}</span>",
        unsafe_allow_html=True,
    )
    if deadline.is_expired:
        st.error(
            "**DEADLINE EXPIRED** — this dispute can no longer be contested. "
            "Route to manual handling."
        )

    tabs = st.tabs([
        "Dispute (Razorpay)", "Order", "Shipment", "Communications",
        "Refund", "Policies", "Documents",
    ])

    with tabs[0]:
        st.caption("Source: Razorpay dispute record")
        st.json({
            "dispute_id": case.dispute_id, "payment_id": case.payment_id,
            "amount": case.amount, "currency": case.currency,
            "reason_code": case.reason_code, "status": case.dispute_status,
            "phase": case.phase, "respond_by": fmt_ts(case.respond_by),
        })

    if evidence is None:
        for tab in tabs[1:]:
            with tab:
                st.warning(
                    "No merchant-side record exists for this payment. "
                    "Insufficient evidence — human review required."
                )
        return

    order = evidence.order
    with tabs[1]:
        st.caption("Source: merchant's own order system (synthetic)")
        st.json({
            "merchant_order_id": order.merchant_order_id, "product": order.product,
            "product_type": order.product_type,
            "amount": fmt_money(order.amount, order.currency),
            "order_status": order.order_status,
            "ordered_at": fmt_ts(order.order_timestamp),
            "shipping_address": order.shipping_address,
        })

    with tabs[2]:
        st.caption("Source: merchant's fulfilment/courier records (synthetic)")
        if evidence.shipment is None:
            st.info(
                f"No shipment record — expected for a **{order.product_type}** product."
                if order.product_type in {"digital", "service"}
                else "No shipment record exists for this physical order."
            )
        else:
            sh = evidence.shipment
            st.json({
                "delivery_status": sh.delivery_status, "tracking_id": sh.tracking_id,
                "courier": sh.courier, "delivered_at": fmt_ts(sh.delivered_at),
                "delivery_location": sh.delivery_location,
                "recipient_confirmation": sh.recipient_confirmation or "NONE ON FILE",
            })

    with tabs[3]:
        st.caption("Source: merchant's support system (synthetic)")
        if not evidence.communications:
            st.info("No customer communications on file.")
        for comm in evidence.communications:
            who = "🧑 Customer" if comm.direction == "inbound" else "🏪 Merchant"
            st.markdown(
                f"**`communication:{comm.id}`** · {fmt_ts(comm.timestamp)} · "
                f"{comm.channel} · {who}"
            )
            st.markdown(f"> {comm.message}")

    with tabs[4]:
        st.caption("Source: merchant's refund records (synthetic)")
        if evidence.refund is None:
            st.info("No refund record exists for this order.")
        else:
            rf = evidence.refund
            st.json({
                "refund_requested": rf.refund_requested, "refund_status": rf.refund_status,
                "refund_amount": fmt_money(rf.refund_amount, order.currency)
                if rf.refund_amount else None,
                "refund_processed_at": fmt_ts(rf.refund_timestamp), "reason": rf.reason,
            })

    with tabs[5]:
        st.caption("Source: merchant's published policies (synthetic)")
        for policy in evidence.policies:
            with st.expander(f"`policy:{policy.policy_type}` (version {policy.version})"):
                st.text(policy.content)

    with tabs[6]:
        st.caption("Source: merchant's document store (synthetic)")
        if not evidence.documents:
            st.info("No supporting documents on file.")
        for doc in evidence.documents:
            st.markdown(
                f"**`document:{doc.id}`** · `{doc.document_type}` · `{doc.filename}`"
            )
            st.caption(doc.description)


def render_ai_assessment(investigation, failure, evidence, case) -> None:
    st.markdown("### 🤖 AI investigation")

    if failure is not None:
        st.error(
            f"**Investigation failed — {failure.get('failure_reason')}**\n\n"
            f"{failure.get('detail')}\n\n"
            "No recommendation is available. This case requires manual review."
        )
        return
    if investigation is None:
        # Defensive fallback only - the caller auto-investigates before this
        # is reached, so this branch should be unreachable in practice.
        st.info("Investigating this dispute…")
        return

    icon, color, blurb = CLASSIFICATION_STYLE[investigation.classification]
    st.markdown(
        f"""
        <div style='border:2px solid {color};border-radius:10px;padding:18px;
                    background:{color}10;text-align:center'>
          <div style='font-size:0.8rem;letter-spacing:0.1em;color:#64748b;
                      font-weight:600'>AI INVESTIGATION — RECOMMENDATION ONLY</div>
          <div style='font-size:2rem;font-weight:700;color:{color};margin:6px 0'>
            {icon} {investigation.classification.replace('_', ' ')}
          </div>
          <div style='font-size:1.05rem;color:#334155'>
            Confidence: <b>{investigation.confidence:.0%}</b>
          </div>
          <div style='color:#475569;margin-top:6px'>{blurb}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(
        f"Model: `{investigation.model}` · investigated "
        f"{fmt_ts(investigation.investigation_timestamp)} · suggested action: "
        f"`{investigation.recommended_action}` (not executed — a human decides)"
    )

    st.markdown("**Executive summary**")
    st.write(investigation.executive_summary)
    st.markdown("**Reasoning**")
    st.write(investigation.reason)

    left, right = st.columns(2)
    with left:
        st.markdown("**Supporting evidence** — every citation resolves to a real record")
        if not investigation.supporting_evidence:
            st.write("_None cited._")
        for citation in investigation.supporting_evidence:
            resolved = resolve_citation(citation.reference, evidence, case)
            with st.expander(f"`{citation.reference}` — {citation.note}"):
                if resolved:
                    st.markdown(resolved)
                else:
                    st.warning(
                        "This citation could not be resolved to a record for display. "
                        "It passed validation at investigation time."
                    )
    with right:
        st.markdown("**Missing evidence**")
        if investigation.missing_evidence:
            for item in investigation.missing_evidence:
                st.markdown(f"- {item}")
        else:
            st.write("_None identified._")

        st.markdown("**Conflicting evidence**")
        if investigation.conflicting_evidence:
            for item in investigation.conflicting_evidence:
                st.markdown(f"- ⚠️ {item}")
        else:
            st.write("_None identified._")

        st.markdown("**Risk factors**")
        if investigation.risk_factors:
            for item in investigation.risk_factors:
                st.markdown(f"- {item}")
        else:
            st.write("_None identified._")


def resolve_citation(reference: str, evidence, case) -> str | None:
    """Turn `document:7` into the actual record it's pointing at.

    The model's citations already got checked against real records back
    when the investigation ran, but I wanted the reviewer to actually SEE
    what's behind each one here too, not just trust that it checks out.
    """
    kind, _, ref_id = reference.partition(":")
    if kind == "dispute":
        return (
            f"Razorpay dispute `{case.dispute_id}` — {fmt_money(case.amount, case.currency)}, "
            f"reason `{case.reason_code}`, status `{case.dispute_status}`."
        )
    if kind == "payment":
        return f"Razorpay payment `{case.payment_id}`."
    if evidence is None:
        return None
    if kind == "order":
        o = evidence.order
        return (
            f"**{o.merchant_order_id}** — {o.product} ({o.product_type}), "
            f"{fmt_money(o.amount, o.currency)}, status `{o.order_status}`, "
            f"ordered {fmt_ts(o.order_timestamp)}."
        )
    if kind == "shipment" and evidence.shipment:
        sh = evidence.shipment
        return (
            f"delivery_status `{sh.delivery_status}`, tracking `{sh.tracking_id}`, "
            f"courier `{sh.courier}`, delivered {fmt_ts(sh.delivered_at)}, "
            f"recipient confirmation: {sh.recipient_confirmation or '**none on file**'}."
        )
    if kind == "refund" and evidence.refund:
        rf = evidence.refund
        return (
            f"refund_status `{rf.refund_status}`, requested: {rf.refund_requested}, "
            f"amount {rf.refund_amount}, reason: {rf.reason or 'not recorded'}."
        )
    if kind == "communication":
        for comm in evidence.communications:
            if str(comm.id) == ref_id:
                who = "Customer" if comm.direction == "inbound" else "Merchant"
                return f"{fmt_ts(comm.timestamp)} · {comm.channel} · **{who}**: “{comm.message}”"
    if kind == "document":
        for doc in evidence.documents:
            if str(doc.id) == ref_id:
                return f"`{doc.document_type}` · `{doc.filename}` — {doc.description}"
    if kind == "policy":
        for policy in evidence.policies:
            if policy.policy_type == ref_id:
                return f"**{policy.policy_type}** (v{policy.version})\n\n{policy.content}"
    return None


def render_evidence_package(case, evidence, investigation, settings) -> None:
    st.markdown("### 📦 Evidence package")

    if investigation is None:
        st.info("Still waiting on the AI's read of this case before I can build a package off it.")
        return

    force_key = f"force_{case.dispute_id}"
    force = st.session_state.get(force_key, False)

    try:
        package = build_evidence_package(
            case, evidence, investigation, settings, force=force
        )
    except EvidenceBuildError as exc:
        st.error("**I'm not building an evidence package for this one.**")
        st.markdown(
            "The investigation came back **NO CASE** — the merchant's own records "
            "don't back up contesting this. I deliberately didn't wire this up to build "
            "a nice-looking package anyway, because that's exactly how someone ends up "
            "rubber-stamping a case that never had a shot."
        )
        with st.expander("Why I'm refusing this one"):
            st.write(str(exc))

        st.warning("**You can override this**")
        st.caption(
            "If you disagree with the AI, you can build the package anyway. The "
            "override is recorded in the package and in the audit log."
        )
        confirm = st.checkbox(
            "I have read the reasoning above and want to override the AI's NO_CASE finding.",
            key=f"confirm_{case.dispute_id}",
        )
        if st.button("Override and build package anyway", disabled=not confirm,
                     type="secondary", key=f"btn_force_{case.dispute_id}"):
            st.session_state[force_key] = True
            st.rerun()
        return

    if force:
        st.warning(
            "⚠️ **HUMAN OVERRIDE ACTIVE** — this package was built against the AI's "
            "NO_CASE recommendation."
        )

    c1, c2, c3 = st.columns(3)
    c1.metric("Evidence categories", len(package.evidence_categories))
    c2.metric("Documents generated", len(package.generated_documents))
    c3.metric(
        "Contest summary",
        f"{package.summary_trace.final_length}/{package.summary_trace.limit}",
    )

    st.markdown("**Razorpay evidence categories** — only those with real records behind them")
    for category, refs in sorted(package.evidence_categories.items()):
        st.markdown(f"- `{category}` ← {', '.join(f'`{r}`' for r in refs)}")

    st.markdown("**Contest summary (draft)**")
    trace = package.summary_trace
    if trace.was_truncated:
        st.warning(
            f"This summary was truncated from {trace.original_length} to "
            f"{trace.final_length} characters to fit Razorpay's {trace.limit}-character "
            "limit. Review it carefully before approving."
        )
    elif trace.was_shortened_by_ai:
        st.info(
            f"Shortened by the model from {trace.original_length} to "
            f"{trace.final_length} characters to fit the {trace.limit}-character limit."
        )
    st.text_area(
        "contest_summary", package.contest_summary, height=120,
        disabled=True, label_visibility="collapsed",
    )

    if package.generated_documents:
        st.markdown("**Generated documents** (PDF — the only format Razorpay accepts)")
        for doc in package.generated_documents:
            st.markdown(f"- `{doc.path.name}` — {doc.document_type}")
            if doc.path.exists():
                st.download_button(
                    f"Download {doc.path.name}", doc.path.read_bytes(),
                    file_name=doc.path.name, mime="application/pdf",
                    key=f"dl_{case.dispute_id}_{doc.path.name}",
                )

    if package.warnings:
        st.markdown("**Warnings**")
        for warning in package.warnings:
            st.warning(warning)

    with st.expander("Explanation letter (draft)"):
        st.text(package.explanation_letter)

    st.session_state[f"package_ready_{case.dispute_id}"] = package.is_submittable


def render_human_review(case, investigation, settings) -> None:
    st.markdown("---")
    st.markdown("### 👤 Human review — the decision is yours")

    settings_db = settings.paths.case_db

    if case.case_state in {"APPROVED", "DRAFTED", "SUBMITTED"}:
        st.success(f"✅ A human has **approved** this case. Current state: `{case.case_state}`")
    elif case.case_state == "OVERRULED":
        st.info("🚫 A human **rejected** the AI recommendation. Current state: `OVERRULED`")
        return
    elif investigation is None:
        st.info("Investigate the case before recording a decision.")
        return

    ai_says = investigation.classification if investigation else "no recommendation"
    st.markdown(
        f"The AI recommends **{ai_says.replace('_', ' ')}**. "
        "That is a recommendation, not a decision — nothing is submitted to Razorpay "
        "unless you explicitly approve it."
    )

    reviewer = st.text_input("Reviewer name", value="demo_reviewer", key=f"rev_{case.dispute_id}")
    note = st.text_area(
        "Decision note (recorded in the audit log)", key=f"note_{case.dispute_id}",
        placeholder="Why are you approving, rejecting, or asking for more work?",
    )

    if case.case_state in {"APPROVED", "DRAFTED", "SUBMITTED"}:
        return

    c1, c2, c3 = st.columns(3)
    decided = None
    if c1.button("✅ Approve recommendation", type="primary", width='stretch',
                 key=f"approve_{case.dispute_id}"):
        decided = "APPROVE"
    if c2.button("🚫 Reject recommendation", width='stretch',
                 key=f"reject_{case.dispute_id}"):
        decided = "REJECT"
    if c3.button("🔁 Request further review", width='stretch',
                 key=f"further_{case.dispute_id}"):
        decided = "REQUEST_FURTHER_REVIEW"

    if decided:
        current = get_case(settings_db, case.dispute_id)
        if current.case_state != "PENDING_HUMAN_REVIEW":
            advance_to_review(settings_db, current, actor=reviewer or "reviewer")
        record_human_decision(
            settings_db, case.dispute_id, decided,
            reviewer=reviewer or "reviewer",
            reason=note or "(no note given)",
            ai_classification=ai_says,
        )
        st.success(f"Recorded: **{decided}** by {reviewer or 'reviewer'}.")
        st.rerun()


def render_contest_draft(case, evidence, investigation, settings) -> None:
    """Contest draft and submission - the last two human gates."""
    if case.case_state not in {"APPROVED", "DRAFTED", "SUBMITTED"}:
        return
    if investigation is None or evidence is None:
        return

    st.markdown("---")
    st.markdown("### 📄 Contest draft")

    try:
        package = build_evidence_package(
            case, evidence, investigation, settings,
            force=st.session_state.get(f"force_{case.dispute_id}", False),
        )
    except EvidenceBuildError:
        st.info("No evidence package exists for this case, so there is nothing to draft.")
        return

    draft = build_local_draft(case, evidence, package, investigation, settings)

    if case.case_state == "SUBMITTED":
        st.success("✅ **SUBMITTED to Razorpay.** Evidence has been sent for bank review.")
    elif case.case_state == "DRAFTED":
        st.info(
            "📝 **Draft saved at Razorpay** (`action=draft`). Evidence is stored but "
            "**has not been submitted** to the bank."
        )
    else:
        st.warning(
            "**DRAFT — NOT SUBMITTED.** Nothing has been sent to Razorpay or any bank."
        )

    c1, c2 = st.columns(2)
    c1.metric("Documents to upload", len(draft.uploadable_documents))
    c2.metric("Summary length", f"{len(draft.payload['summary'])}/"
                                f"{settings.contest_summary_max_chars}")

    st.markdown("**Evidence categories in the payload** (Razorpay field → documents)")
    for category, ids in sorted(draft.document_ids_by_category.items()):
        st.markdown(f"- `{category}` — {len(ids)} document(s)")
    if draft.unsupported_categories:
        st.caption(
            "Cited but not sendable as documents (Razorpay evidence fields take "
            "uploaded files, not record references): "
            + ", ".join(f"`{c}`" for c in sorted(draft.unsupported_categories))
        )

    with st.expander("Inspect the exact payload that would be sent to Razorpay"):
        st.json(draft.payload)

    if draft.blocked_reason:
        st.error(f"🚫 **Submission blocked.** {draft.blocked_reason}")
        st.caption(
            "I put this check in `contest_service.assert_submittable`, not just here "
            "in the UI, so it can't be skipped by accident — a simulated dispute "
            "never even reaches Razorpay's API."
        )
        return

    st.markdown("---")
    st.markdown("#### 👤 Human submission — the final gate")
    reviewer = st.text_input(
        "Your name (recorded against the submission)",
        value="demo_reviewer", key=f"submitter_{case.dispute_id}",
    )

    if case.case_state == "APPROVED":
        if st.button("💾 Save draft at Razorpay (action=draft)",
                     key=f"savedraft_{case.dispute_id}"):
            try:
                save_draft_to_razorpay(case, evidence, package, investigation,
                                       actor=reviewer or "reviewer", settings=settings)
                st.success("Draft saved at Razorpay. Not submitted.")
                st.rerun()
            except (ContestError, SubmissionBlocked) as exc:
                st.error(f"Draft failed — case state unchanged. {exc}")

    if case.case_state == "DRAFTED":
        st.error(
            "**Submitting is irreversible.** The evidence goes to the issuing bank "
            "for a decision. Confirm below to enable the submit button."
        )
        confirmed = st.checkbox(
            "I have reviewed the draft above and authorise submission to Razorpay.",
            key=f"confirm_submit_{case.dispute_id}",
        )
        if st.button("🚀 Submit contest to Razorpay", type="primary",
                     disabled=not confirmed, key=f"submit_{case.dispute_id}"):
            try:
                submit_contest(case, evidence, package, investigation,
                               actor=reviewer or "reviewer", human_confirmed=True,
                               settings=settings)
                st.success("Contest submitted to Razorpay.")
                st.rerun()
            except (ContestError, SubmissionBlocked) as exc:
                st.error(f"Submission failed — case state unchanged. {exc}")

    attempts = get_contest_attempts(settings.paths.case_db, case.dispute_id)
    if attempts:
        with st.expander(f"Contest history ({len(attempts)} attempt(s))"):
            for attempt in attempts:
                status = "✅ succeeded" if attempt["succeeded"] else "❌ failed"
                st.markdown(
                    f"**{fmt_ts(attempt['created_at'])}** · `{attempt['action']}` · "
                    f"{status} · by `{attempt['actor']}`"
                )
                if attempt["error"]:
                    st.caption(attempt["error"])


def render_audit(case, settings) -> None:
    with st.expander("🧾 Audit log — every state change and human action"):
        entries = review_history(settings.paths.case_db, case.dispute_id)
        if not entries:
            st.write("_No audit entries._")
        for entry in entries:
            st.markdown(
                f"**{fmt_ts(entry['timestamp'])}** · `{entry['action']}` · "
                f"actor: `{entry['actor']}` · "
                f"`{entry['previous_state']}` → `{entry['new_state']}`"
            )
            if entry.get("reason"):
                st.caption(entry["reason"])


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main() -> None:
    render_header()

    try:
        settings = get_settings()
    except ConfigError as exc:
        st.error(f"Configuration error: {exc}")
        return

    render_integration_proof(settings)

    with st.sidebar:
        st.header("Demo controls")
        st.caption(
            "All cases below are **synthetic demo data**. Razorpay provides no API "
            "to create a test dispute, so dispute events are simulated against "
            "Razorpay's documented schema."
        )
        if st.button("🔄 Load / reset demo cases", width='stretch'):
            import subprocess
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent.parent
                                     / "scripts" / "seed_merchant_db.py"), "--reset"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                st.success("Demo cases loaded.")
            else:
                st.error(f"Seeding failed:\n{result.stderr[:400]}")
            st.rerun()

        pending = [
            c for c in list_cases(settings.paths.case_db)
            if get_latest_investigation(settings.paths.case_db, c.dispute_id) is None
        ]
        if pending:
            st.caption(f"{len(pending)} case(s) not yet investigated.")
            if st.button(f"🤖 Investigate all {len(pending)} pending", width='stretch'):
                progress = st.progress(0.0)
                for i, case in enumerate(pending):
                    result = investigate_dispute(
                        case.dispute_id, settings.paths.case_db,
                        settings.paths.merchant_db, settings,
                    )
                    save_investigation(settings.paths.case_db, result)
                    if result.succeeded and case.case_state == "INGESTED":
                        advance_to_review(settings.paths.case_db, case, actor="ai_investigation")
                    progress.progress((i + 1) / len(pending))
                st.success(f"Investigated {len(pending)} case(s).")
                st.rerun()

        st.markdown("---")
        st.caption(
            f"Model: `{settings.ai.model}`\n\n"
            f"Deadline thresholds: critical <{settings.deadlines.critical_hours}h, "
            f"warning <{settings.deadlines.warning_hours}h"
        )
        st.markdown("---")
        st.caption(
            "**One rule I built this whole thing around:** the AI can flag a case as "
            "strong as it wants, but it can't submit anything itself. A human has to "
            "approve first, every time."
        )

        st.markdown("---")
        with st.expander("📖 Quick glossary"):
            st.markdown(
                "**🟢 Strong case** — merchant records disprove the customer's claim.\n\n"
                "**🟡 Weak case** — some evidence, but real gaps. Judgment call.\n\n"
                "**🔴 No case** — merchant can't show it met its obligation. Don't contest.\n\n"
                "---\n"
                "**Deadline colours** — 🔴 Critical: less than "
                f"{settings.deadlines.critical_hours}h left · 🟡 Warning: less than "
                f"{settings.deadlines.warning_hours}h left · grey: plenty of time.\n\n"
                "---\n"
                "**Case status**, in order:\n"
                + "\n".join(f"- `{state}` — {label}" for state, label in CASE_STATE_LABELS.items())
                + "\n\n"
                "**Draft vs. Submit** — a draft saves evidence at Razorpay; "
                "nothing reaches the bank until a human explicitly submits."
            )

    cases = list_cases(settings.paths.case_db)
    summaries = [build_case_summary(c, settings.paths.case_db, settings) for c in cases]

    render_overview(summaries)
    st.markdown("---")
    selected = render_queue(summaries)
    if not selected:
        return

    case = get_case(settings.paths.case_db, selected)
    evidence = get_case_evidence(settings.paths.merchant_db, payment_id=case.payment_id)
    investigation, failure = load_investigation(settings.paths.case_db, selected)

    st.markdown("---")
    st.header(f"Case {case.dispute_id}")
    render_case_facts(case, evidence, settings)

    st.markdown("---")
    if investigation is None and failure is None:
        # Automatic — a reviewer's job is to decide, not to remember to click
        # "investigate" on every case. This fires once per case: after the
        # result is saved, the case_state advance below makes it findable on
        # the next rerun and this branch stops being true.
        with st.spinner("🤖 Investigating against merchant records..."):
            result = investigate_dispute(
                case.dispute_id, settings.paths.case_db, settings.paths.merchant_db, settings
            )
            save_investigation(settings.paths.case_db, result)
            if result.succeeded and case.case_state == "INGESTED":
                advance_to_review(settings.paths.case_db, case, actor="ai_investigation")
        st.rerun()

    render_ai_assessment(investigation, failure, evidence, case)

    if investigation is not None and evidence is not None:
        st.markdown("---")
        render_evidence_package(case, evidence, investigation, settings)

    render_human_review(case, investigation, settings)
    render_contest_draft(case, evidence, investigation, settings)
    st.markdown("---")
    render_audit(case, settings)


main()
