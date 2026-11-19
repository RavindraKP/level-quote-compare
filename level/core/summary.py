"""Figures derived from a scenario result, for the analyst and the Award page.

Every total, count, difference and exposure the screen shows is computed here, so the model
only ever quotes numbers (non-negotiable 2). Pure functions; no database access.
"""

from __future__ import annotations

from decimal import Decimal

from level.types import CellState, ScenarioResult

UNCONFIRMED = {CellState.INFERRED, CellState.NEEDS_REVIEW, CellState.CONFLICT}
EXPOSURE_SHARE = Decimal("0.10")  # T11: "how much moves if this price is 10% off"


def by_vendor(result: ScenarioResult) -> dict[int, dict]:
    """Per vendor: lines won, annual value, and how much of that value is unconfirmed."""
    out: dict[int, dict] = {}
    for a in result.allocation:
        if a.vendor_id is None or a.line_total_inr is None:
            continue
        row = out.setdefault(a.vendor_id, {"lines": 0, "total_inr": Decimal(0),
                                           "unconfirmed_lines": 0,
                                           "unconfirmed_total_inr": Decimal(0)})
        row["lines"] += 1
        row["total_inr"] += a.line_total_inr
        if a.cell_state in UNCONFIRMED:
            row["unconfirmed_lines"] += 1
            row["unconfirmed_total_inr"] += a.line_total_inr
    return out


def vendor_split(result: ScenarioResult) -> list[dict]:
    """The award split by vendor, largest first: lines won, annual value, share of the
    option's total (a percentage, None when the option has no total) and prices not yet
    verified. Lines with no price are left out (never counted as zero)."""
    total = result.total_inr
    rows = [{"vendor_id": vid, "lines": r["lines"], "total_inr": r["total_inr"],
             "share_pct": (r["total_inr"] / total * 100) if total else None,
             "unconfirmed_lines": r["unconfirmed_lines"]}
            for vid, r in by_vendor(result).items()]
    return sorted(rows, key=lambda r: (-r["total_inr"], r["vendor_id"]))


def risk(result: ScenarioResult) -> dict:
    """The doubt this result rests on: unconfirmed allocated lines and the rupees at stake.

    exposure_inr = 10% of the annual value of every allocated line whose price is unconfirmed.
    lines_winner_changes = lines where a 10% move in an unconfirmed price changes the winner.
    """
    unconfirmed = [a for a in result.allocation
                   if a.cell_state in UNCONFIRMED and a.line_total_inr is not None]
    exposure = sum((a.line_total_inr * EXPOSURE_SHARE for a in unconfirmed), Decimal(0))
    flips = sorted({s.rfx_line_id for s in result.sensitivity if s.winner_changes})
    return {
        "unconfirmed_line_ids": [a.rfx_line_id for a in unconfirmed],
        "unconfirmed_count": len(unconfirmed),
        "unconfirmed_value_inr": sum((a.line_total_inr for a in unconfirmed), Decimal(0)),
        "exposure_inr": exposure,
        "lines_winner_changes": flips,
    }


def headline(result: ScenarioResult) -> dict:
    """The few numbers shown side by side on the Award page."""
    r = risk(result)
    return {
        "total_inr": result.total_inr,
        "last_year_total_inr": result.last_year_total_inr,
        "savings_inr": result.savings_inr,
        "savings_pct": result.savings_pct,
        "vendors_used": len(result.vendors_used),
        "lines_allocated": sum(1 for a in result.allocation if a.vendor_id is not None),
        "lines_without_quote": len(result.coverage_gaps),
        "unconfirmed_count": r["unconfirmed_count"],
        "exposure_inr": r["exposure_inr"],
        "lines_winner_changes": len(r["lines_winner_changes"]),
    }


def compare(results: list[ScenarioResult]) -> list[dict]:
    """Each result's headline, plus its difference from the first one (a minus first)."""
    if not results:
        return []
    base = headline(results[0])["total_inr"]
    out = []
    for res in results:
        h = headline(res)
        known = h["total_inr"] is not None and base is not None  # an option with no total
        h["total_minus_first_inr"] = h["total_inr"] - base if known else None
        h["pct_vs_first"] = (h["total_inr"] - base) / base * 100 if known and base else None
        out.append(h)
    return out
