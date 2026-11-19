"""Award memo (DOCX) for one scenario: built by code from the stored scenario result.

Page one: the recommendation and, before anything else, the prices it relies on that are
not confirmed, with the rupees at stake (PLAN §6 "Award gating"). Then the allocation, the
basis and assumptions, the caveats, and an appendix with every review action taken.
"""

from __future__ import annotations

import io
from decimal import Decimal

import sqlalchemy as sa
from docx import Document
from docx.enum.text import WD_BREAK

from level import copy, db
from level.agents.analyst import audit
from level.agents.analyst.tools import _lines, _vendors, load_scenario, scenario_name
from level.core import summary


def _inr(value) -> str:
    """Totals in whole rupees with Indian grouping: '₹51,13,523' (UX guide). Display only."""
    return copy.inr(None if value is None else Decimal(str(value)))


def _table(doc, header: list[str], rows: list[list[str]]) -> None:
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Light Grid Accent 1"
    for cell, text in zip(t.rows[0].cells, header, strict=True):
        cell.text = text
    for r in rows:
        for cell, text in zip(t.add_row().cells, r, strict=True):
            cell.text = str(text)


def award_memo(rfx_id: int, scenario_id: int) -> bytes:
    row, result = load_scenario(scenario_id)
    vendors = _vendors(rfx_id)
    lines = {ln["id"]: ln for ln in _lines(rfx_id)}
    head = summary.headline(result)
    risk = summary.risk(result)
    with db.connect() as conn:
        rfx = conn.execute(sa.select(db.rfx).where(db.rfx.c.id == rfx_id)).mappings().one()
        assumptions = conn.execute(sa.select(db.assumption)).mappings().all()
    # This event's changes only, in words; the checks' own runs are left out (UX audit B8).
    changes = [r for r in audit.trail(rfx_id) if r["actor"] != "system"]

    doc = Document()
    doc.add_heading(f"Award recommendation: {rfx['title']}", 0)
    name = row["name"] or scenario_name(result.params, vendors)
    doc.add_paragraph(f"Award option: {copy.plain(name)}")
    doc.add_paragraph(
        f"Annual value {_inr(head['total_inr'])} across {head['lines_allocated']} lines, "
        f"{copy.plural(head['vendors_used'], 'vendor')}: "
        f"{', '.join(vendors[v] for v in result.vendors_used) or 'none'}.")
    if result.savings_inr is not None:
        doc.add_paragraph(f"Against last year's prices on the same lines "
                          f"({_inr(result.last_year_total_inr)}): saving "
                          f"{_inr(result.savings_inr)} ({copy.pct(result.savings_pct)}).")
    if result.coverage_gaps:
        doc.add_paragraph("Lines with no eligible price (not in the total): " + ", ".join(
            str(lines[i]["line_no"]) for i in result.coverage_gaps))

    doc.add_heading("Not yet verified", level=1)
    shaky = [a for a in result.allocation if a.rfx_line_id in set(risk["unconfirmed_line_ids"])]
    if not shaky:
        doc.add_paragraph("Every allocated price is verified.")
    else:
        doc.add_paragraph(
            f"{copy.plural(risk['unconfirmed_count'], 'price')} in this award not yet "
            f"verified, worth {_inr(risk['unconfirmed_value_inr'])} a year. If each were 10% "
            f"off, the total would move by up to {_inr(risk['exposure_inr'])}. A 10% move "
            f"changes the winner on {copy.plural(len(risk['lines_winner_changes']), 'line')}.")
        _table(doc, ["Line", "Vendor", "Status", "Annual value", "10% exposure"],
               [[lines[a.rfx_line_id]["line_no"], vendors.get(a.vendor_id, ""),
                 copy.STATE_LABEL[a.cell_state] if a.cell_state else "", _inr(a.line_total_inr),
                 _inr(a.line_total_inr * summary.EXPOSURE_SHARE)] for a in shaky])
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    doc.add_heading("Allocation", level=1)
    _table(doc, ["Line", "Description", "Vendor", "Unit price", "Annual qty", "Line total",
                 "Status"],
           [[lines[a.rfx_line_id]["line_no"], lines[a.rfx_line_id]["description"],
             vendors.get(a.vendor_id, "no eligible price") if a.vendor_id else "no eligible price",
             copy.unit_price(a.unit_price_inr), copy.number(a.annual_qty), _inr(a.line_total_inr),
             copy.STATE_LABEL[a.cell_state] if a.cell_state else "—"]
            for a in result.allocation])

    doc.add_heading("Basis and assumptions", level=1)
    p = result.params
    doc.add_paragraph(f"Prices in {rfx['base_currency']}, ex-GST, per each line's unit; basis "
                      f"{'delivered' if p.price_basis == 'landed' else 'ex-works'}"
                      f"{' with payment-terms carrying cost' if p.include_payment_terms else ''}.")
    # A rate set for this option is the one stated; the event's rate is named beside it.
    overrides = {f"fx.{k[:3].upper()}{(k[3:] or rfx['base_currency']).upper()}": v
                 for k, v in (p.fx_override or {}).items()}
    for a in assumptions:
        source = (f"source: {a['source'] or 'not stated'}, "
                  f"as of {copy.date(a['as_of']) if a['as_of'] else 'not stated'}")
        if a["key"] in overrides:
            doc.add_paragraph(f"{copy.assumption_label(a['key'])}: "
                              f"{copy.assumption_value(a['key'], overrides[a['key']])} for this "
                              f"option (the event's rate is "
                              f"{copy.assumption_value(a['key'], a['value'])}; {source})",
                              style="List Bullet")
            continue
        doc.add_paragraph(f"{copy.assumption_label(a['key'])}: "
                          f"{copy.assumption_value(a['key'], a['value'])} ({source})",
                          style="List Bullet")
    doc.add_heading("Caveats (attached by the calculation, not written by AI)", level=1)
    parts = copy.caveat_parts(result.caveats)
    for c in parts["main"] + parts["more"] + parts["basis"]:
        doc.add_paragraph(c, style="List Bullet")
    if parts["terms"]:
        doc.add_heading(copy.terms_noted(len(parts["terms"])), level=2)
        for c in parts["terms"]:
            doc.add_paragraph(c, style="List Bullet")

    doc.add_heading("Appendix: changes made during review", level=1)
    if not changes:
        doc.add_paragraph("No review actions recorded.")
    for r in changes:
        doc.add_paragraph(f"{r['when']} · {r['who']} · {r['what']}"
                          + (f": {r['details']}" if r["details"] else ""), style="List Bullet")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
