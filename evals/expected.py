"""Expected answers computed from ground truth by code (never typed by hand).

An independent, deliberately simple re-computation of the award arithmetic, so the evals
can check level/core and the analyst without trusting them. Bases:
- "ex_works": the per-unit price, INR, ex-GST, before freight;
- "landed":   per-unit price + stated freight (unknown freight -> no landed price);
- carrying=True adds payment-term carrying cost: price x rate x (60 - days) / 365,
  rate = assumption cost_of_capital (paying earlier than the requested 60 days costs us).
"""

from __future__ import annotations

import re
from decimal import Decimal
from itertools import combinations

from evals.truth import cells, dec

REQUESTED_DAYS = 60


def usd_quoted(cell: dict) -> Decimal | None:
    match = re.match(r"USD ([\d.]+)", cell.get("as_quoted") or "")
    return Decimal(match.group(1)) if match else None


def price(truth: dict, key: str, line_no: int, basis: str = "ex_works",
          fx: Decimal | None = None, carrying: bool = False) -> Decimal | None:
    vendor = truth["vendors"][key]
    c = cells(truth, key)[line_no]
    unit = dec(c["unit_price_inr"])
    if unit is None:
        return None
    if fx is not None and usd_quoted(c) is not None:
        unit = usd_quoted(c) * fx
    value = unit
    if basis == "landed":
        freight = dec(c["freight_inr"])
        if freight is None:
            return None
        value = unit + freight
    if carrying:
        days = vendor.get("payment_days")
        if days is None:
            return None
        rate = Decimal(truth["assumptions"]["cost_of_capital"]["value"])
        value = value * (1 + rate * (REQUESTED_DAYS - days) / 365)
    return value


def eligible(truth: dict) -> list[str]:
    """Vendors that pass every gating question."""
    return [k for k, v in truth["vendors"].items() if v["gating"]["overall"] == "pass"]


def qty(truth: dict) -> dict[int, int]:
    return {ln["line_no"]: ln["annual_qty"] for ln in truth["lines"]}


def split(truth: dict, vendors: list[str], basis: str = "ex_works", lines=None,
          **kw) -> dict:
    """Cheapest vendor per line. Lines with no price among `vendors` are gaps (not zero)."""
    q = qty(truth)
    lines = lines or list(q)
    total, allocation, gaps = Decimal("0"), {}, []
    for no in lines:
        offers = [(price(truth, k, no, basis, **kw), k) for k in vendors]
        offers = [o for o in offers if o[0] is not None]
        if not offers:
            gaps.append(no)
            continue
        best_price, best = min(offers)
        allocation[no] = best
        total += best_price * q[no]
    return {"total": total, "allocation": allocation, "gaps": gaps,
            "vendors_used": sorted(set(allocation.values()))}


def single(truth: dict, key: str, basis: str = "ex_works", **kw) -> dict:
    return split(truth, [key], basis, **kw)


def capped(truth: dict, vendors: list[str], k: int, basis: str = "landed", **kw) -> dict:
    """Best split using at most k vendors, covering the same lines as the unconstrained split."""
    free = split(truth, vendors, basis, **kw)
    covered = list(free["allocation"])
    best = None
    for n in range(1, k + 1):
        for subset in combinations(vendors, n):
            result = split(truth, list(subset), basis, lines=covered, **kw)
            if result["gaps"]:
                continue
            if best is None or result["total"] < best["total"]:
                best = result
    return best


def last_year_total(truth: dict, lines=None) -> Decimal:
    q = qty(truth)
    lines = lines if lines is not None else list(q)
    by_no = {ln["line_no"]: ln for ln in truth["lines"]}
    return sum((Decimal(by_no[n]["last_price"]) * q[n] for n in lines), Decimal("0"))


def cheapest_vendor(truth: dict, line_no: int, vendors=None, basis: str = "ex_works") -> str:
    vendors = vendors or list(truth["vendors"])
    return split(truth, vendors, basis, lines=[line_no])["allocation"][line_no]
