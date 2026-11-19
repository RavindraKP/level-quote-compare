"""Excel exports: the comparison (as quoted, per unit, delivered, issues, assumptions) and an
award option. Returns bytes so a page can hand them to a download button.

Every heading and label comes from level/copy.py, so an export reads like the screens."""

from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from level import copy
from level.core.cells import Snapshot, compute_cells
from level.types import CellState, ScenarioResult

STATE_FILL = {
    CellState.NEEDS_REVIEW: "FFF2CC", CellState.CONFLICT: "F4CCCC",
    CellState.NOT_QUOTED: "E7E6E6", CellState.AWAITING: "F3F3F3", CellState.INFERRED: "DDEBF7",
}


def _header(ws, values: list[str]) -> None:
    ws.append(values)
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)


def _grid_sheet(wb, title: str, snapshot: Snapshot, cells, value_of) -> None:
    ws = wb.create_sheet(title)
    vendors = snapshot.vendors
    _header(ws, ["Line", "Description", "Unit", "Annual qty"] + [v.name for v in vendors])
    by_key = {(c.vendor_id, c.rfx_line_id): c for c in cells}
    for ln in snapshot.lines:
        row = [ln.line_no, ln.description, ln.canonical_uom,
               float(ln.annual_qty) if ln.annual_qty is not None else None]
        states = []
        for v in vendors:
            c = by_key[(v.id, ln.id)]
            value = value_of(c)
            # Never show a number for a cell with no price: the state label goes there instead.
            row.append(value if value is not None else copy.STATE_LABEL[c.state])
            states.append(c.state)
        ws.append(row)
        for i, state in enumerate(states):
            if state in STATE_FILL:
                ws.cell(ws.max_row, 5 + i).fill = PatternFill("solid", fgColor=STATE_FILL[state])


def comparison_xlsx(snapshot: Snapshot) -> bytes:
    """Sheets: As quoted, Per unit (INR ex-works), Delivered, Issues, Assumptions, Notes."""
    cells = compute_cells(snapshot)
    wb = Workbook()
    wb.remove(wb.active)
    _grid_sheet(wb, "As quoted", snapshot, cells, lambda c: c.as_quoted)
    _grid_sheet(wb, "Per unit (INR)", snapshot, cells,
                lambda c: _price(c.unit_price_inr))
    _grid_sheet(wb, "Delivered (INR)", snapshot, cells,
                lambda c: _price(c.landed_price_inr))

    names = {v.id: v.name for v in snapshot.vendors}
    line_no = {ln.id: ln.line_no for ln in snapshot.lines}
    ws = wb.create_sheet("Issues")
    _header(ws, ["Vendor", "Line", "Issue", "Priority", "Status", "Settled by", "Details"])
    for f in snapshot.flags:
        ws.append([names.get(f.vendor_id, ""), line_no.get(f.rfx_line_id),
                   copy.ISSUE_TITLE[f.type], copy.SEVERITY_LABEL[f.severity],
                   "Open" if f.state == "open" else "Settled",
                   copy.CLOSED_BY.get(str(f.closed_by or ""), "").capitalize(),
                   _words(f.reason)])

    ws = wb.create_sheet("Assumptions")
    _header(ws, ["Assumption", "Value"])
    for k, v in sorted(snapshot.assumptions.items()):
        ws.append([copy.assumption_label(k), copy.assumption_value(k, v)])
    ws = wb.create_sheet("Price notes")
    _header(ws, ["Vendor", "Line", "Status", "Note"])
    for c in cells:
        ws.append([names[c.vendor_id], line_no[c.rfx_line_id], copy.STATE_LABEL[c.state],
                   _words(c.reason)])
    return _bytes(wb)


def scenario_xlsx(result: ScenarioResult, snapshot: Snapshot) -> bytes:
    """Allocation, totals and caveats of one award option."""
    names = {v.id: v.name for v in snapshot.vendors}
    lines = {ln.id: ln for ln in snapshot.lines}
    wb = Workbook()
    ws = wb.active
    ws.title = "Allocation"
    _header(ws, ["Line", "Description", "Vendor", "Unit price (₹)", "Annual qty",
                 "Line total (₹)", "Status"])
    for a in result.allocation:
        ln = lines[a.rfx_line_id]
        ws.append([ln.line_no, ln.description, names.get(a.vendor_id, "no eligible price"),
                   _price(a.unit_price_inr), _f(a.annual_qty), _whole(a.line_total_inr),
                   copy.STATE_LABEL[a.cell_state] if a.cell_state else ""])
    ws = wb.create_sheet("Summary")
    p = result.params
    approach = {"single_vendor": "One vendor for every line",
                "cheapest_per_line": "Cheapest eligible price on each line",
                "capped_vendors": f"Cheapest per line, at most {p.max_vendors or 2} vendors"}
    for label, value in [  # words and rounded figures, never raw values (UX audit M17)
        ("How lines are given out", approach.get(p.strategy, copy.plain(p.strategy))),
        ("Price basis", "Delivered" if p.price_basis == "landed" else "Ex-works"),
        ("Annual value", copy.inr(result.total_inr)),
        ("Last year, same lines", copy.inr(result.last_year_total_inr)),
        ("Saving against last year", copy.inr(result.savings_inr)),
        ("Saving %", copy.pct(result.savings_pct)),
        ("Vendors used", ", ".join(names[v] for v in result.vendors_used)),
    ]:
        ws.append([label, value])
    ws = wb.create_sheet("Caveats")
    parts = copy.caveat_parts(result.caveats)
    for c in parts["main"] + parts["more"] + parts["basis"]:
        ws.append([c])
    for c in parts["terms"]:
        ws.append([f"Term noted: {c}"])
    ws = wb.create_sheet("Sensitivity")
    _header(ws, ["Line", "Vendor", "Would a 10% move change the winner?", "Exposure (₹)"])
    for s in result.sensitivity:
        ws.append([lines[s.rfx_line_id].line_no, names[s.vendor_id],
                   "Yes" if s.winner_changes else "No", round(_f(s.exposure_inr))])
    return _bytes(wb)


def _f(value):
    return None if value is None else float(value)


def _price(value):
    """A unit price to two decimals, as on screen (display rounding only)."""
    return None if value is None else round(float(value), 2)


def _words(reason: str) -> str:
    """A reason in the buyer's words, with dates as 31 Jan 2025."""
    return copy.indian_numbers(copy._iso_dates(copy.plain(reason)))


def _whole(value):
    """A total in whole rupees (UX guide: totals to whole rupees). Display rounding only."""
    return None if value is None else round(float(value))


def _bytes(wb: Workbook) -> bytes:
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
