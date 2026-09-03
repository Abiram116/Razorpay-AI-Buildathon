"""Renders evidence and case reports as PDFs.

Two reasons this exists, one of them forced by Razorpay:

1. Razorpay's Documents API (`POST /v1/documents`, purpose=dispute_evidence)
   accepts ONLY PDF, PNG and JPG (verified in Phase 1). The merchant's own
   records in this prototype are plain text, so a text record can never be
   uploaded as-is - it has to be rendered into an acceptable format first.
2. A human reviewer needs one readable document that shows the whole case:
   the dispute, the evidence, what the AI concluded, and what is missing.

Nothing here invents content. Every value rendered comes from the merchant
database, the Razorpay dispute record, or the already-validated
InvestigationResult.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .database import CaseRecord
from .investigation_schema import InvestigationResult
from .merchant_db import CaseEvidence, EvidenceDocument

logger = logging.getLogger(__name__)

# The rupee sign is not in reportlab's built-in Helvetica font and renders as
# a black box. Amounts are written as "INR 15,000.00" throughout instead.
_CURRENCY_AS_TEXT = True


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontSize=16, spaceAfter=4, alignment=TA_LEFT,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontSize=9,
            textColor=colors.HexColor("#666666"), spaceAfter=10,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontSize=11, spaceBefore=12, spaceAfter=4,
            textColor=colors.HexColor("#1a1a1a"),
        ),
        "body": ParagraphStyle("body", parent=base["Normal"], fontSize=9.5, leading=13),
        "mono": ParagraphStyle(
            "mono", parent=base["Normal"], fontName="Courier", fontSize=8.5, leading=11,
        ),
        "banner": ParagraphStyle(
            "banner", parent=base["Normal"], fontSize=9, textColor=colors.white,
            backColor=colors.HexColor("#b45309"), borderPadding=5, spaceAfter=10,
        ),
    }


def _fmt_money(amount_minor: int, currency: str) -> str:
    return f"{currency} {amount_minor / 100:,.2f}"


def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "not recorded"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# reportlab's built-in fonts (Helvetica/Courier) are Latin-1 only. Anything
# outside that renders as a black box. LLM output routinely contains typographic
# Unicode - measured in real Groq output here: U+2011 non-breaking hyphen,
# U+2013 en dash, U+202F narrow no-break space - so it must be transliterated
# rather than passed through.
_UNICODE_FALLBACKS = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2026": "...",
    "\u2032": "'", "\u2033": '"', "\u00a0": " ", "\u202f": " ", "\u2009": " ",
    "\u200b": "", "\u2212": "-", "\u20b9": "INR ",
}


def _to_renderable(text: str) -> str:
    """Make text safe for a Latin-1 built-in font.

    Known typographic characters are transliterated to their ASCII
    equivalents; anything else outside Latin-1 is dropped rather than left to
    render as a black box in a document a human has to read and sign off on.
    """
    out = "".join(_UNICODE_FALLBACKS.get(ch, ch) for ch in str(text))
    return out.encode("latin-1", errors="ignore").decode("latin-1")


def _escape(text: str) -> str:
    """reportlab Paragraph parses a mini-HTML dialect, so raw & < > in
    merchant data (customer messages especially) would corrupt the layout or
    raise. Escape, then make the result renderable in a Latin-1 font."""
    escaped = (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return _to_renderable(escaped)


def _simulated_banner(is_simulated: bool, styles: dict) -> list:
    if not is_simulated:
        return []
    return [
        Paragraph(
            "SIMULATED TEST DISPUTE &mdash; this case was generated locally for "
            "demonstration. It is not a real Razorpay dispute and this document "
            "must not be submitted to any bank.",
            styles["banner"],
        )
    ]


def _kv_table(rows: list[tuple[str, str]], styles: dict) -> Table:
    data = [
        [Paragraph(f"<b>{_escape(k)}</b>", styles["body"]), Paragraph(_escape(v), styles["body"])]
        for k, v in rows
    ]
    table = Table(data, colWidths=[45 * mm, 120 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#dddddd")),
    ]))
    return table


@dataclass(frozen=True)
class GeneratedDocument:
    """A PDF I produced, and where it came from."""

    path: Path
    document_type: str
    description: str
    source_filename: str | None
    is_simulated: bool

    @property
    def mime_type(self) -> str:
        return "application/pdf"


def generate_evidence_document_pdf(
    document: EvidenceDocument,
    case_evidence: CaseEvidence,
    output_dir: Path,
    source_dir: Path | None = None,
) -> GeneratedDocument:
    """Render one merchant evidence record as an uploadable PDF.

    If the underlying source file exists on disk its contents are included
    verbatim. If it does not, the PDF is still produced from the database
    record but explicitly states the source file was not found - it never
    silently pretends the file was there.
    """
    styles = _styles()
    order = case_evidence.order
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{Path(document.filename).stem}.pdf"

    story: list = []
    story += _simulated_banner(order.is_simulated, styles)
    story.append(Paragraph(f"Evidence: {_escape(document.document_type)}", styles["title"]))
    story.append(Paragraph(
        f"Merchant order {_escape(order.merchant_order_id)} &middot; "
        f"generated {_fmt_ts(int(datetime.now(tz=timezone.utc).timestamp()))}",
        styles["subtitle"],
    ))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 6))

    story.append(_kv_table([
        ("Document type", document.document_type),
        ("Record id", f"document:{document.id}" if document.id is not None else "unsaved"),
        ("Source file", document.filename),
        ("Merchant order", order.merchant_order_id),
        ("Product", order.product),
        ("Order amount", _fmt_money(order.amount, order.currency)),
    ], styles))

    story.append(Paragraph("Description (merchant record)", styles["h2"]))
    story.append(Paragraph(_escape(document.description), styles["body"]))

    story.append(Paragraph("Source document contents", styles["h2"]))
    source_path = (source_dir / document.filename) if source_dir else None
    if source_path and source_path.exists():
        for line in source_path.read_text().splitlines():
            story.append(Paragraph(_escape(line) if line.strip() else "&nbsp;", styles["mono"]))
    else:
        story.append(Paragraph(
            f"Source file '{_escape(document.filename)}' was not found on disk. "
            "This PDF was generated from the merchant database record only.",
            styles["body"],
        ))

    SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"Evidence {document.document_type} - {order.merchant_order_id}",
    ).build(story)

    logger.info("generated evidence PDF %s", out_path.name)
    return GeneratedDocument(
        path=out_path, document_type=document.document_type,
        description=document.description, source_filename=document.filename,
        is_simulated=order.is_simulated,
    )


def generate_explanation_letter_pdf(
    case: CaseRecord,
    case_evidence: CaseEvidence,
    investigation: InvestigationResult,
    explanation_text: str,
    output_dir: Path,
) -> GeneratedDocument:
    """The merchant's explanation letter - a Razorpay evidence category in
    its own right (`explanation_letter`)."""
    styles = _styles()
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"explanation-letter-{case.dispute_id}.pdf"

    story: list = []
    story += _simulated_banner(case.is_simulated, styles)
    story.append(Paragraph("Merchant Explanation Letter", styles["title"]))
    story.append(Paragraph(
        f"Dispute {_escape(case.dispute_id)} &middot; "
        f"order {_escape(case_evidence.order.merchant_order_id)}",
        styles["subtitle"],
    ))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 8))

    for paragraph in explanation_text.split("\n\n"):
        if paragraph.strip():
            story.append(Paragraph(_escape(paragraph.strip()), styles["body"]))
            story.append(Spacer(1, 6))

    SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"Explanation letter - {case.dispute_id}",
    ).build(story)

    logger.info("generated explanation letter %s", out_path.name)
    return GeneratedDocument(
        path=out_path, document_type="explanation_letter",
        description="Merchant explanation letter prepared for this dispute.",
        source_filename=None, is_simulated=case.is_simulated,
    )


def generate_billing_proof_pdf(
    case_evidence: CaseEvidence,
    case: CaseRecord,
    output_dir: Path,
) -> GeneratedDocument:
    """Render the order record as a billing proof PDF.

    `evidence_builder.select_evidence_categories` cites `billing_proof` for
    every order (the order record IS proof of what was billed), but an order
    row is not a file. Without this, billing_proof would cite a real record
    and then have no document behind it in every single case - exactly the
    gap the policy/explanation-letter PDFs were added to close in Phase 7,
    just missed for this category.
    """
    styles = _styles()
    order = case_evidence.order
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"billing-proof-{order.merchant_order_id}.pdf"

    story: list = []
    story += _simulated_banner(order.is_simulated, styles)
    story.append(Paragraph("Billing Record", styles["title"]))
    story.append(Paragraph(
        f"Merchant order {_escape(order.merchant_order_id)} &middot; "
        f"payment {_escape(case.payment_id)}",
        styles["subtitle"],
    ))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 8))
    story.append(_kv_table([
        ("Merchant order ID", order.merchant_order_id),
        ("Customer ID", order.customer_id),
        ("Product", f"{order.product} ({order.product_type})"),
        ("Amount billed", _fmt_money(order.amount, order.currency)),
        ("Order status", order.order_status),
        ("Ordered at", _fmt_ts(order.order_timestamp)),
        ("Billing address", order.billing_address or "not recorded"),
        ("Razorpay payment ID", case.payment_id),
    ], styles))

    SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"Billing proof - {order.merchant_order_id}",
    ).build(story)

    logger.info("generated billing proof PDF %s", out_path.name)
    return GeneratedDocument(
        path=out_path, document_type="billing_proof",
        description=f"Order and billing record for {order.merchant_order_id}.",
        source_filename=None, is_simulated=order.is_simulated,
    )


def generate_communications_transcript_pdf(
    case_evidence: CaseEvidence,
    case: CaseRecord,
    output_dir: Path,
) -> GeneratedDocument:
    """Render the full communications log as a `customer_communication` PDF.

    `evidence_builder.select_evidence_categories` cites `customer_communication`
    against `order:...` whenever any communications exist, but that citation
    is a record reference, not a file - the same class of gap as billing_proof.
    Rendering the transcript closes it independent of whether any individual
    message also happens to have its own uploaded document.
    """
    styles = _styles()
    order = case_evidence.order
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"communications-{order.merchant_order_id}.pdf"

    story: list = []
    story += _simulated_banner(order.is_simulated, styles)
    story.append(Paragraph("Customer Communications Log", styles["title"]))
    story.append(Paragraph(f"Merchant order {_escape(order.merchant_order_id)}", styles["subtitle"]))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 8))

    for comm in case_evidence.communications:
        who = "Customer" if comm.direction == "inbound" else "Merchant"
        story.append(Paragraph(
            f"<b>[communication:{comm.id}]</b> {_fmt_ts(comm.timestamp)} &middot; "
            f"{_escape(comm.channel)} &middot; {who}",
            styles["body"],
        ))
        story.append(Paragraph(f"&ldquo;{_escape(comm.message)}&rdquo;", styles["body"]))
        story.append(Spacer(1, 6))

    SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"Communications - {order.merchant_order_id}",
    ).build(story)

    logger.info("generated communications transcript PDF %s", out_path.name)
    return GeneratedDocument(
        path=out_path, document_type="customer_communication",
        description=f"Full customer communications log for {order.merchant_order_id}.",
        source_filename=None, is_simulated=order.is_simulated,
    )


def generate_policy_pdf(
    policy,
    case: CaseRecord,
    output_dir: Path,
) -> GeneratedDocument:
    """Render a merchant policy as an uploadable PDF.

    Razorpay has evidence categories for the refund/cancellation policy and
    the terms and conditions, but they take uploaded document ids - the
    policy text sitting in the database is not a file. Without this the
    policy categories would be cited in the package and then silently vanish
    from the contest payload.
    """
    styles = _styles()
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"policy-{policy.policy_type}.pdf"

    story: list = []
    story += _simulated_banner(case.is_simulated, styles)
    story.append(Paragraph(
        _escape(policy.policy_type.replace("_", " ").title()), styles["title"],
    ))
    story.append(Paragraph(
        f"Version {_escape(policy.version)} &middot; effective from "
        f"{_fmt_ts(policy.effective_from)}",
        styles["subtitle"],
    ))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 8))
    for paragraph in policy.content.split("\n\n"):
        if paragraph.strip():
            story.append(Paragraph(_escape(paragraph.strip()), styles["body"]))
            story.append(Spacer(1, 6))

    SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"{policy.policy_type} v{policy.version}",
    ).build(story)

    category = (
        "term_and_conditions" if policy.policy_type == "terms_and_conditions"
        else "refund_cancellation_policy"
    )
    logger.info("generated policy PDF %s", out_path.name)
    return GeneratedDocument(
        path=out_path, document_type=category,
        description=f"Merchant {policy.policy_type} (version {policy.version}) as published.",
        source_filename=None, is_simulated=case.is_simulated,
    )


def generate_case_report_pdf(
    case: CaseRecord,
    case_evidence: CaseEvidence,
    investigation: InvestigationResult,
    output_dir: Path,
    contest_summary: str | None = None,
    included_categories: dict[str, list[str]] | None = None,
) -> GeneratedDocument:
    """The human reviewer's document: the whole case on paper.

    This is for the review workflow (Phase 6), not for the bank - it contains
    the AI's reasoning and confidence, which are internal.
    """
    styles = _styles()
    order = case_evidence.order
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"chargeback-case-{case.dispute_id}.pdf"

    story: list = []
    story += _simulated_banner(case.is_simulated, styles)
    story.append(Paragraph("Chargeback Defence Report", styles["title"]))
    story.append(Paragraph(
        f"Dispute {_escape(case.dispute_id)} &middot; generated "
        f"{_fmt_ts(int(datetime.now(tz=timezone.utc).timestamp()))} &middot; "
        "AI recommendation, pending human review",
        styles["subtitle"],
    ))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#cccccc")))

    story.append(Paragraph("Dispute (reported by Razorpay)", styles["h2"]))
    story.append(_kv_table([
        ("Dispute ID", case.dispute_id),
        ("Payment ID", case.payment_id),
        ("Disputed amount", _fmt_money(case.amount, case.currency)),
        ("Reason code", case.reason_code),
        ("Status / phase", f"{case.dispute_status} / {case.phase}"),
        ("Respond by", _fmt_ts(case.respond_by)),
        ("Data source", "Simulated test dispute" if case.is_simulated else "Razorpay webhook"),
    ], styles))

    story.append(Paragraph("Merchant order record", styles["h2"]))
    story.append(_kv_table([
        ("Merchant order", order.merchant_order_id),
        ("Product", f"{order.product} ({order.product_type})"),
        ("Order amount", _fmt_money(order.amount, order.currency)),
        ("Order status", order.order_status),
        ("Ordered at", _fmt_ts(order.order_timestamp)),
    ], styles))

    story.append(Paragraph("Delivery / fulfilment", styles["h2"]))
    if case_evidence.shipment is None:
        story.append(Paragraph(
            f"No shipment record exists for this order (product type: "
            f"{_escape(order.product_type)}).", styles["body"],
        ))
    else:
        sh = case_evidence.shipment
        story.append(_kv_table([
            ("Delivery status", sh.delivery_status),
            ("Tracking ID", sh.tracking_id or "not recorded"),
            ("Courier", sh.courier or "not recorded"),
            ("Delivered at", _fmt_ts(sh.delivered_at)),
            ("Recipient confirmation", sh.recipient_confirmation or "NONE ON FILE"),
        ], styles))

    if case_evidence.communications:
        story.append(Paragraph("Customer communications", styles["h2"]))
        for comm in case_evidence.communications:
            who = "Customer" if comm.direction == "inbound" else "Merchant"
            story.append(Paragraph(
                f"<b>[communication:{comm.id}]</b> {_fmt_ts(comm.timestamp)} &middot; "
                f"{_escape(comm.channel)} &middot; {who}: &ldquo;{_escape(comm.message)}&rdquo;",
                styles["body"],
            ))
            story.append(Spacer(1, 3))

    story.append(Paragraph("Refund history", styles["h2"]))
    if case_evidence.refund is None:
        story.append(Paragraph("No refund record exists for this order.", styles["body"]))
    else:
        rf = case_evidence.refund
        story.append(_kv_table([
            ("Refund requested", str(rf.refund_requested)),
            ("Refund status", rf.refund_status),
            ("Refund amount",
             _fmt_money(rf.refund_amount, order.currency) if rf.refund_amount else "none"),
            ("Processed at", _fmt_ts(rf.refund_timestamp)),
        ], styles))

    story.append(PageBreak())

    story.append(Paragraph("AI investigation (recommendation only)", styles["h2"]))
    story.append(_kv_table([
        ("Classification", investigation.classification),
        ("Confidence", f"{investigation.confidence:.0%}"),
        ("Suggested action", f"{investigation.recommended_action} - human decision required"),
        ("Model", investigation.model),
        ("Investigated at", _fmt_ts(investigation.investigation_timestamp)),
    ], styles))

    story.append(Paragraph("Summary", styles["h2"]))
    story.append(Paragraph(_escape(investigation.executive_summary), styles["body"]))
    story.append(Paragraph("Reasoning", styles["h2"]))
    story.append(Paragraph(_escape(investigation.reason), styles["body"]))

    story.append(Paragraph("Evidence cited", styles["h2"]))
    if investigation.supporting_evidence:
        for citation in investigation.supporting_evidence:
            story.append(Paragraph(
                f"<b>{_escape(citation.reference)}</b> &mdash; {_escape(citation.note)}",
                styles["body"],
            ))
            story.append(Spacer(1, 2))
    else:
        story.append(Paragraph("No supporting evidence was cited.", styles["body"]))

    story.append(Paragraph("Missing evidence", styles["h2"]))
    if investigation.missing_evidence:
        for item in investigation.missing_evidence:
            story.append(Paragraph(f"&bull; {_escape(item)}", styles["body"]))
    else:
        story.append(Paragraph("None identified.", styles["body"]))

    story.append(Paragraph("Conflicting evidence", styles["h2"]))
    if investigation.conflicting_evidence:
        for item in investigation.conflicting_evidence:
            story.append(Paragraph(f"&bull; {_escape(item)}", styles["body"]))
    else:
        story.append(Paragraph("None identified.", styles["body"]))

    if investigation.risk_factors:
        story.append(Paragraph("Risk factors", styles["h2"]))
        for item in investigation.risk_factors:
            story.append(Paragraph(f"&bull; {_escape(item)}", styles["body"]))

    if included_categories:
        story.append(Paragraph("Evidence package (Razorpay categories)", styles["h2"]))
        for category, items in sorted(included_categories.items()):
            story.append(Paragraph(
                f"<b>{_escape(category)}</b>: {_escape(', '.join(items))}", styles["body"],
            ))
            story.append(Spacer(1, 2))

    if contest_summary:
        story.append(Paragraph("Proposed contest summary (draft)", styles["h2"]))
        story.append(Paragraph(
            f"{len(contest_summary)} characters &mdash; this text would be sent as the "
            "dispute evidence summary if a human approves.",
            styles["subtitle"],
        ))
        story.append(Paragraph(_escape(contest_summary), styles["body"]))

    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#cccccc")))
    story.append(Paragraph(
        "This report is an AI-generated recommendation. No dispute has been "
        "contested or submitted. A human reviewer must approve before any "
        "submission to Razorpay.",
        styles["subtitle"],
    ))

    SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"Chargeback defence report - {case.dispute_id}",
    ).build(story)

    logger.info("generated case report %s", out_path.name)
    return GeneratedDocument(
        path=out_path, document_type="others",
        description="Internal chargeback defence report for human review.",
        source_filename=None, is_simulated=case.is_simulated,
    )
