"""Award scenarios (PLAN §7): who gets which line, what it costs, and what to be careful of.

`solve(snapshot, params)` is pure; `run_scenario(rfx_id, params)` loads the snapshot first.
What-ifs (FX rate, conditional discounts, dropping a vendor) are parameters: the cells are
recomputed in memory from the same claims, and nothing stored changes.

Strategies:
- single_vendor: one vendor takes every line it quoted. Without a named vendor, the one
  covering the most lines, then the cheapest, is chosen.
- cheapest_per_line: each line goes to its cheapest eligible price.
- capped_vendors: try every subset of at most `max_vendors` vendors (5 vendors -> 31
  subsets), give each line to the cheapest vendor in the subset, keep the best subset.
"""

from __future__ import annotations

import re
from decimal import Decimal
from itertools import combinations

from level.core.cells import Snapshot, compute_cells, in_totals, load_snapshot
from level.core.gating import vendor_gating
from level.types import Allocation, CellState, ScenarioParams, ScenarioResult, SensitivityItem

UNCONFIRMED = {CellState.INFERRED, CellState.NEEDS_REVIEW, CellState.CONFLICT}
PESSIMISTIC_UPLIFT = Decimal("1.10")
SENSITIVITY = Decimal("0.10")
VALIDITY_WORDS = ("exchange", "fx", "currency", "valid")
# Caveats that only note a vendor's term start with this, so a page can fold them away
# from the caveats that change the number (UX audit M2).
TERM_NOTE = "Term noted: "


def _n(n: int, word: str) -> str:
    """'1 line', '3 lines': plural forms written in code, never 'line(s)'."""
    return f"{n} {word if n == 1 else word + 's'}"


def _lines(nos: list[int]) -> str:
    """'line 6' or 'lines 3, 5 and 6'."""
    text = [str(n) for n in nos]
    if len(text) == 1:
        return f"line {text[0]}"
    return f"lines {', '.join(text[:-1])} and {text[-1]}"


def run_scenario(rfx_id: int, params: ScenarioParams) -> ScenarioResult:
    return solve(load_snapshot(rfx_id), params)


def _gating_result(snapshot: Snapshot, vendor_id: int) -> str:
    verdicts = snapshot.gating.get(vendor_id, [])
    return "pass" if not verdicts else vendor_gating(verdicts)


def solve(snapshot: Snapshot, params: ScenarioParams) -> ScenarioResult:
    names = {v.id: v.name for v in snapshot.vendors}
    caveats: list[str] = []
    cells = compute_cells(snapshot, fx_override=params.fx_override,
                          apply_conditional_discounts=params.apply_conditional_discounts,
                          include_payment_terms=params.include_payment_terms)
    by_key = {(c.vendor_id, c.rfx_line_id): c for c in cells}
    lines = [ln for ln in snapshot.lines if ln.id not in params.excluded_line_ids]
    qty = {ln.id: ln.annual_qty or Decimal(0) for ln in snapshot.lines}
    if params.excluded_line_ids:
        caveats.append(f"{_n(len(params.excluded_line_ids), 'line')} left out at your request.")

    # 1. Which vendors may win anything.
    pool = []
    for v in snapshot.vendors:
        if params.vendor_ids is not None and v.id not in params.vendor_ids:
            continue
        if all(by_key[(v.id, ln.id)].state == CellState.AWAITING for ln in snapshot.lines):
            caveats.append(f"{v.name} has not replied, so is not in this award option.")
            continue
        gate = _gating_result(snapshot, v.id)
        if params.require_gating_pass and gate == "fail":
            caveats.append(f"{v.name} excluded: does not meet a requirement.")
            continue
        if gate == "unclear" and params.require_gating_pass and params.exclude_unclear_gating:
            caveats.append(f"{v.name} excluded: some answers to the requirements are unclear, "
                            "so they are not counted as met.")
            continue
        if gate == "unclear":
            caveats.append(f"{v.name} included, although some answers to the requirements are "
                            "unclear.")
        pool.append(v.id)
    if params.strategy == "single_vendor" and params.single_vendor_id is not None:
        if params.single_vendor_id not in pool:
            caveats.append(f"{names.get(params.single_vendor_id, 'Chosen vendor')} is not "
                           f"eligible under these settings.")
            pool = []
        else:
            pool = [params.single_vendor_id]

    # 2. The price each eligible vendor would charge on each line, under these settings.
    price: dict[tuple[int, int], Decimal] = {}
    landed_unknown: dict[int, int] = {}
    held: dict[int, list[int]] = {}
    for v in pool:
        for ln in lines:
            c = by_key[(v, ln.id)]
            value = c.landed_price_inr if params.price_basis == "landed" else c.unit_price_inr
            if params.price_basis == "landed" and c.unit_price_inr is not None and value is None:
                landed_unknown[v] = landed_unknown.get(v, 0) + 1
            if value is None:
                continue
            if not in_totals(c):
                held.setdefault(v, []).append(ln.line_no)
                continue
            if c.state in UNCONFIRMED:
                if params.unconfirmed == "exclude":
                    continue
                if params.unconfirmed == "pessimistic":
                    value *= PESSIMISTIC_UPLIFT
            price[(v, ln.id)] = value
    for v, nos in held.items():
        caveats.append(f"{names[v]}: the price on {_lines(nos)} looks like a different unit "
                       f"(about 10, 100 or 1000 times other quotes); left out of this option "
                       f"until it is checked.")
    for v, n in landed_unknown.items():
        caveats.append(f"{names[v]}: delivered price unknown on {_n(n, 'line')} (freight not "
                       f"stated); left out of the delivered comparison there.")

    # 3. Allocate.
    if params.strategy == "single_vendor":
        options = [[v] for v in pool]
    elif params.strategy == "capped_vendors":
        k = params.max_vendors or 2
        options = [list(s) for n in range(1, min(k, len(pool)) + 1)
                   for s in combinations(pool, n)]
    else:
        options = [pool]
    best_alloc, best_total = None, None
    for subset in options or [[]]:
        alloc = _cheapest(lines, subset, price)
        covered = sum(1 for a in alloc.values() if a is not None)
        total = sum((price[(a, ln)] * qty[ln] for ln, a in alloc.items()
                     if a is not None), Decimal(0))
        score = (-covered, total)
        if best_total is None or score < best_total:
            best_alloc, best_total = alloc, score
    assert best_alloc is not None

    allocation, total, last_total, compare_total, gaps = [], Decimal(0), Decimal(0), Decimal(0), []
    for ln in lines:
        v = best_alloc.get(ln.id)
        if v is None:
            gaps.append(ln.id)
            allocation.append(Allocation(rfx_line_id=ln.id, vendor_id=None, unit_price_inr=None,
                                         annual_qty=qty[ln.id], line_total_inr=None,
                                         cell_state=None))
            continue
        unit = price[(v, ln.id)]
        line_total = unit * qty[ln.id]
        total += line_total
        if ln.last_price is not None:
            last_total += ln.last_price * qty[ln.id]
            compare_total += line_total
        allocation.append(Allocation(rfx_line_id=ln.id, vendor_id=v, unit_price_inr=unit,
                                     annual_qty=qty[ln.id], line_total_inr=line_total,
                                     cell_state=by_key[(v, ln.id)].state))
    vendors_used = sorted({a.vendor_id for a in allocation if a.vendor_id is not None})

    # 4. Caveats attached by code (T10).
    line_no = {ln.id: ln.line_no for ln in snapshot.lines}
    if gaps:
        caveats.append(f"{_n(len(gaps), 'line')} with no eligible price, not in the total: "
                       f"{_lines([line_no[g] for g in gaps])}.")
    shaky = [a for a in allocation if a.cell_state in UNCONFIRMED]
    if shaky:
        caveats.append(f"{_n(len(shaky), 'price')} in this option not yet verified (assumed, "
                       f"to check or mismatch): {_lines([line_no[a.rfx_line_id] for a in shaky])}.")
    if params.unconfirmed == "pessimistic":
        caveats.append("Prices not yet verified raised by 10% (cautious setting).")
    if params.price_basis == "landed":
        caveats.append("Delivered price = price + freight" + (
            " + the cost of each vendor's payment terms at the cost of capital."
            if params.include_payment_terms else "; payment terms not counted."))
    caveats += _assumption_caveats(by_key, allocation, params, snapshot.assumptions)
    caveats += _term_caveats(snapshot, vendors_used, names, params)

    if not vendors_used:
        # No line has a price on this basis: the total is unknown, not zero (rule 4).
        caveats.append("No line has a price on this basis, so this option has no total.")
    last_year = last_total if compare_total else None
    savings = (last_total - compare_total) if compare_total else None
    pct = (savings / last_total * 100) if savings is not None and last_total else None
    return ScenarioResult(
        params=params, allocation=allocation, vendors_used=vendors_used,
        total_inr=total if vendors_used else None,
        last_year_total_inr=last_year, savings_inr=savings, savings_pct=pct,
        coverage_gaps=gaps, caveats=caveats,
        sensitivity=_sensitivity(allocation, price, by_key, pool, vendors_used, params),
    )


def _cheapest(lines, vendors, price) -> dict[int, int | None]:
    """Each line -> the cheapest vendor in `vendors` with a price (ties: lower id)."""
    alloc = {}
    for ln in lines:
        offers = [(price[(v, ln.id)], v) for v in vendors if (v, ln.id) in price]
        alloc[ln.id] = min(offers)[1] if offers else None
    return alloc


def _assumption_caveats(by_key, allocation, params, reference: dict | None = None) -> list[str]:
    """How prices were converted, in this option's own terms: a rate the buyer set for this
    option is the one named, with the reference rate beside it, never both as if in force."""
    reference = reference or {}
    overridden = {pair[:3].upper(): rate for pair, rate in (params.fx_override or {}).items()}
    used: dict[str, str] = {}
    for a in allocation:
        if a.vendor_id is None:
            continue
        for k, v in by_key[(a.vendor_id, a.rfx_line_id)].assumptions.items():
            if k.startswith("fx.") or k in ("gst_rate", "cost_of_capital"):
                used[k] = v
    out = []
    for k, v in sorted(used.items()):
        if k.startswith("fx.") and k[3:6] in overridden:
            continue  # said once below, as the rate set for this option
        if k.startswith("fx."):
            out.append(f"{k[3:6]} prices converted at {_money(v, k[6:])} per {k[3:6]}.")
        elif k == "gst_rate":
            out.append(f"Tax-inclusive prices reduced to ex-GST at {Decimal(v) * 100:.0f}% GST.")
        else:
            out.append(f"Payment terms costed at {Decimal(v) * 100:.0f}% a year.")
    if params.fx_override:
        for pair, rate in sorted(params.fx_override.items()):  # never the raw dict (B5)
            code, base = pair[:3].upper(), (pair[3:] or "INR").upper()
            ref = reference.get(f"fx.{code}{base}")
            was = (f", in place of the reference rate of {_money(ref, base)}"
                   if ref not in (None, "") else "")
            out.append(f"{code} to {base} taken as {_money(rate, base)} for this option{was}.")
    return out


def _money(value, currency: str) -> str:
    """A rate for a caveat: '₹90.00' for rupees, else '90.00 USD'. Display rounding only."""
    amount = f"{Decimal(str(value)):.2f}"
    return f"₹{amount}" if currency == "INR" else f"{amount} {currency}"


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def term_text(t) -> str:
    """A term in the vendor's words, with its value and conditions added only when the text
    does not already say them ("valid until 30 June 2027" is not followed by the date again).
    """
    parts: list[str] = []
    for x in (t.text, t.value, t.conditions):
        x = (x or "").strip()
        if x and not any(_squash(x) in _squash(p) for p in parts):
            parts.append(x)
    text = " ".join(parts)
    return re.sub(r"^\s*\d+[.)]\s+", "", text)  # a list number ("4. Rates valid ...")


def _term_caveats(snapshot, vendors_used, names, params) -> list[str]:
    out = []
    for t in snapshot.terms:
        if t.vendor_id not in vendors_used:
            continue
        text = term_text(t)
        if t.type == "discount" and t.is_conditional:
            state = "applied" if params.apply_conditional_discounts else "not applied"
            out.append(f"{TERM_NOTE}{names[t.vendor_id]}: conditional discount {state}: {text}")
        elif t.type == "validity" or (t.type == "other" and any(
                w in text.lower() for w in VALIDITY_WORDS)):
            out.append(f"{TERM_NOTE}{names[t.vendor_id]}: {text}")
        elif t.type == "tooling":
            out.append(f"{TERM_NOTE}{names[t.vendor_id]}: one-time charge, not in unit prices: "
                       f"{text}")
    return out


def _sensitivity(allocation, price, by_key, pool, vendors_used, params) -> list[SensitivityItem]:
    """T11: would a +/-10% move in an unconfirmed price change who wins the line?

    Rivals are the vendors this scenario could use: the whole pool, or the chosen subset
    under a vendor cap. A single-vendor award has no rival on any line.
    """
    if params.strategy == "capped_vendors":
        pool = vendors_used
    items = []
    for a in allocation:
        if a.vendor_id is None:
            continue
        line = a.rfx_line_id
        win_price = price[(a.vendor_id, line)]
        rivals = {v: price[(v, line)] for v in pool if v != a.vendor_id and (v, line) in price}
        if params.strategy == "single_vendor":
            rivals = {}  # one vendor takes everything: nobody else can win a line
        for v in [a.vendor_id, *rivals]:
            cell = by_key[(v, line)]
            if cell.state not in UNCONFIRMED:
                continue
            p = price[(v, line)]
            if v == a.vendor_id:
                changes = any(r < p * (1 + SENSITIVITY) for r in rivals.values())
            else:
                changes = p * (1 - SENSITIVITY) < win_price
                if not changes:
                    continue
            items.append(SensitivityItem(rfx_line_id=line, vendor_id=v, winner_changes=changes,
                                         exposure_inr=p * SENSITIVITY * a.annual_qty))
    return items
