"""The RFx document sent to vendors as a PDF attachment.

Built by code from draft.snapshot(), which holds the request only: lines, specs, quantities,
delivery places, terms and the questionnaire. No last-year price, incumbent or other vendor's
data is in the snapshot, so none can reach the document (non-negotiable 10).

PDF via PyMuPDF's HTML Story engine (the HTML -> PDF approach the dataset generator uses):
pip-installed, no system binaries, works on Windows.
"""

from __future__ import annotations

import html
import io
import re
from decimal import Decimal, InvalidOperation

import pymupdf
import sqlalchemy as sa

from level import copy, db
from level.agents.copilot.draft import snapshot, standard_terms

# Terms shown in the document's header rather than in the terms table.
HEADER_TERMS = ("ref", "title", "category", "buyer", "due")
# These, in this order and each once, are the "key terms" in the email body.
KEY_TERMS = ("price_basis", "currency", "payment_days", "delivery")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")

CSS = """
body { font-family: sans-serif; font-size: 9pt; color: #222; }
h1 { font-size: 15pt; margin: 0 0 4pt 0; }
h2 { font-size: 11pt; margin: 12pt 0 4pt 0; }
p { margin: 2pt 0; }
table { border-collapse: collapse; width: 100%; }
th { background-color: #e8eef0; text-align: left; font-size: 8pt; }
th, td { border: 1px solid #b0b8bc; padding: 2pt 3pt; vertical-align: top; }
td.num { text-align: right; }
.muted { color: #666; }
"""


def _rfx(rfx_id: int) -> dict:
    with db.connect() as conn:
        return dict(conn.execute(sa.select(db.rfx).where(db.rfx.c.id == rfx_id))
                    .mappings().one())


def term_text(value) -> str:
    """A term value as plain words: lists joined, ISO dates as '24 Feb 2025'."""
    if isinstance(value, list):
        return ", ".join(map(str, value))
    text = str(value)
    return copy.date(text) if ISO_DATE.match(text) else text


def key_terms(terms: dict) -> list[str]:
    """'Label: value' lines for the email body: price basis, currency, payment and delivery,
    each once, under the names used now (an older 'basis' is the price basis)."""
    terms = standard_terms(terms)
    return [f"{copy.field_label(k)}: {term_text(terms[k])}" for k in KEY_TERMS
            if terms.get(k) not in (None, "", [])]


def filename(rfx_id: int) -> str:
    """'RFx-SF-PKG-FY26-014.pdf': the event number, safe as a file name on any system."""
    name = re.sub(r"[^A-Za-z0-9-]+", "-", copy.event_number(_rfx(rfx_id))).strip("-")
    return (name if name.upper().startswith("RFX") else f"RFx-{name}") + ".pdf"


def _spec(spec: dict | None) -> str:
    return "; ".join(f"{copy.field_label(k)}: {v}" for k, v in (spec or {}).items())


def _qty(value) -> str:
    try:
        return copy.number(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return str(value or "—")


def rfx_html(rfx_id: int) -> str:
    """The document as HTML. Every value is escaped: draft text is typed by people."""
    e = html.escape
    s = snapshot(rfx_id)
    rfx = _rfx(rfx_id)
    terms = s["terms"]
    buyer = copy.buyer_company(rfx) or copy.buyer_name()
    head = [f"<h1>Request for quotation: {e(s['title'])}</h1>",
            f"<p>Reference: {e(copy.event_number(rfx))}</p>",
            f"<p>From: {e(buyer)}</p>"]
    if s["category"]:
        head.append(f"<p>Category: {e(s['category'])}</p>")
    if terms.get("due"):
        head.append(f"<p><b>Please send your quotation by {e(term_text(terms['due']))}.</b></p>")

    scope = (f"<h2>1. Scope</h2><p>This request covers {len(s['lines'])} line"
             f"{'s' if len(s['lines']) != 1 else ''}. Please quote every line in the unit shown "
             "for it. If you do not offer a line, say so. Reply in any format: the attached "
             "Excel template, your own quotation, or an email.</p>")

    rows = "".join(
        f"<tr><td class='num'>{line['line_no']}</td><td>{e(line['description'] or '')}</td>"
        f"<td>{e(_spec(line['spec']))}</td><td>{e(line['canonical_uom'] or '')}</td>"
        f"<td class='num'>{e(_qty(line['annual_qty']))}</td><td>{e(line['plant'] or '')}</td></tr>"
        for line in s["lines"])
    lines = ("<h2>2. Lines</h2><table><tr><th>Line</th><th>Item</th><th>Specification</th>"
             "<th>Quote per</th><th>Annual qty</th><th>Deliver to</th></tr>"
             f"{rows or '<tr><td colspan=6>No lines yet.</td></tr>'}</table>")

    term_rows = "".join(f"<tr><td>{e(copy.field_label(k))}</td><td>{e(term_text(v))}</td></tr>"
                        for k, v in terms.items() if k not in HEADER_TERMS)
    terms_html = ("<h2>3. Terms</h2>" + (f"<table>{term_rows}</table>" if term_rows
                                          else "<p class='muted'>No terms set.</p>"))

    q_rows = "".join(
        f"<tr><td class='num'>Q{q['q_no']}</td><td>{e(q['section'] or 'General')}</td>"
        f"<td>{e(q['text'] or '')}</td><td>{'Yes' if q['is_gating'] else 'No'}</td></tr>"
        for q in s["questions"])
    questions = ("<h2>4. Questionnaire</h2>" + (
        "<table><tr><th>Q</th><th>Section</th><th>Question</th><th>Required to qualify</th>"
        f"</tr>{q_rows}</table>" if q_rows else "<p class='muted'>No questions.</p>"))

    return "".join(head) + scope + lines + terms_html + questions


def rfx_pdf(rfx_id: int) -> bytes:
    """The RFx as an A4 PDF."""
    story = pymupdf.Story(html=rfx_html(rfx_id), user_css=CSS)
    rect = pymupdf.paper_rect("a4")
    # Written to memory, not a temp file: on Windows a temp file still held open by the
    # writer cannot be deleted, which crashed the page.
    buffer = io.BytesIO()
    writer = pymupdf.DocumentWriter(buffer)
    more = True
    while more:  # one A4 page at a time until the story is placed
        device = writer.begin_page(rect)
        more, _ = story.place(rect + (36, 36, -36, -36))
        story.draw(device)
        writer.end_page()
    writer.close()
    return buffer.getvalue()
