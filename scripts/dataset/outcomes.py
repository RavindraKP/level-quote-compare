"""The designed outcomes in docs/DATASET.md, checked directly on ground truth.

Each check returns (ok, detail). tests/test_dataset.py asserts all of them for several seeds.
These are simple re-computations on truth, independent of level/core.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import combinations


def _cells(truth: dict, key: str) -> dict[int, dict]:
    return {c["line_no"]: c for c in truth["vendors"][key]["cells"]}


def price(truth: dict, key: str, line_no: int, basis: str) -> Decimal | None:
    """basis 'ex_works' = unit price; 'landed' = unit + freight (None when freight unknown)."""
    c = _cells(truth, key)[line_no]
    value = c["unit_price_inr"] if basis == "ex_works" else c["landed_price_inr"]
    return None if value is None else Decimal(value)


def eligible(truth: dict) -> list[str]:
    return [k for k, v in truth["vendors"].items() if v["gating"]["overall"] == "pass"]


def cheapest(truth: dict, vendors: list[str], line_no: int, basis: str) -> str | None:
    offers = [(price(truth, k, line_no, basis), k) for k in vendors]
    offers = [o for o in offers if o[0] is not None]
    return min(offers)[1] if offers else None


def split_total(truth: dict, vendors: list[str], basis: str, lines: list[int]) -> Decimal | None:
    """Cheapest-per-line total over `lines`; None if some line has no price among `vendors`."""
    qty = {ln["line_no"]: ln["annual_qty"] for ln in truth["lines"]}
    total = Decimal("0")
    for no in lines:
        best = cheapest(truth, vendors, no, basis)
        if best is None:
            return None
        total += price(truth, best, no, basis) * qty[no]
    return total


def d_cheapest_but_ineligible(truth: dict) -> tuple[bool, str]:
    keys = list(truth["vendors"])
    wins = [ln["line_no"] for ln in truth["lines"]
            if cheapest(truth, keys, ln["line_no"], "ex_works") == "D"]
    verdict = truth["vendors"]["D"]["gating"]["overall"]
    ok = len(wins) > len(truth["lines"]) / 2 and verdict == "fail"
    return ok, f"D cheapest on {len(wins)}/{len(truth['lines'])} lines; gating {verdict}"


def c_wins_ex_works_loses_landed(truth: dict) -> tuple[bool, str]:
    elig = eligible(truth)
    flipped = [ln["line_no"] for ln in truth["lines"]
               if cheapest(truth, elig, ln["line_no"], "ex_works") == "C"
               and price(truth, "C", ln["line_no"], "landed") is not None
               and cheapest(truth, elig, ln["line_no"], "landed") != "C"]
    return len(flipped) >= 3, f"C wins ex-works but loses landed on lines {flipped}"


def cap_of_two_costs_slightly_more(truth: dict) -> tuple[bool, str]:
    """Among eligible vendors, landed basis: best two-vendor split vs unconstrained split."""
    elig = eligible(truth)
    lines = [ln["line_no"] for ln in truth["lines"]
             if cheapest(truth, elig, ln["line_no"], "landed")]
    free = split_total(truth, elig, "landed", lines)
    pairs = [split_total(truth, list(p), "landed", lines) for p in combinations(elig, 2)]
    capped = min(t for t in pairs if t is not None)
    extra = (capped - free) / free * 100
    ok = Decimal("0") < extra <= Decimal("1.5")
    return ok, f"cap of two costs {extra:.2f}% more ({capped - free:,.0f} INR)"


def per_box_trap_flips_award(truth: dict) -> tuple[bool, str]:
    keys = list(truth["vendors"])
    part = next(ln for ln in truth["lines"] if ln["kind"] == "partition")["line_no"]
    winner = cheapest(truth, keys, part, "ex_works")
    misread = price(truth, "D", part, "ex_works") * 100  # "per box" read as per set
    others = [price(truth, k, part, "ex_works") for k in keys if k != "D"]
    worst_other = max(p for p in others if p is not None)
    ok = winner == "D" and misread > worst_other
    return ok, f"line {part}: true winner {winner}; misread D price {misread} vs {worst_other}"


def per_kg_reconciles(truth: dict) -> tuple[bool, str]:
    """Per-kg quotes x ref_weight_g give exactly the per-piece truth."""
    weights = {ln["line_no"]: Decimal(ln["ref_weight_g"]) for ln in truth["lines"]}
    bad = []
    for key in ("D", "E"):
        rates = truth["vendors"][key]["doc"]["rates_per_kg"]
        for ln in truth["lines"]:
            c = _cells(truth, key)[ln["line_no"]]
            if "/kg" not in (c["as_quoted"] or "").replace(" ", ""):
                continue
            expected = Decimal(rates[str(ln["spec"]["ply"])]) * weights[ln["line_no"]] / 1000
            if Decimal(c["unit_price_inr"]) != expected.quantize(Decimal("0.0001")):
                bad.append((key, ln["line_no"]))
    return not bad, f"mismatches: {bad}"


def b_quotes_27(truth: dict) -> tuple[bool, str]:
    n = sum(c["quoted"] for c in truth["vendors"]["B"]["cells"])
    return n == 27, f"B quotes {n} of 30"


def e_reference_from_own_records(truth: dict) -> tuple[bool, str]:
    """Every 'same as last year' line of E resolves from E's own FY25 record, contract first."""
    own = {}
    for h in truth["history"]:
        if h["vendor"] == "E" and (h["source"] == "contract" or h["line_no"] not in own):
            own[h["line_no"]] = h["unit_price"]
    ref = [c for c in truth["vendors"]["E"]["cells"] if c["as_quoted"] == "same as last year"]
    bad = [c["line_no"] for c in ref if not c["quoted"] or c["line_no"] not in own
           or Decimal(c["unit_price_inr"]) != Decimal(own[c["line_no"]])]
    return bool(ref) and not bad, f"{len(ref)} reference lines, mismatched {bad}"


def gating_as_designed(truth: dict) -> tuple[bool, str]:
    got = {k: v["gating"]["overall"] for k, v in truth["vendors"].items()}
    want = {"A": "pass", "B": "pass", "C": "pass", "D": "fail", "E": "unclear"}
    return got == want, f"verdicts {got}"


ALL = [d_cheapest_but_ineligible, c_wins_ex_works_loses_landed, cap_of_two_costs_slightly_more,
       per_box_trap_flips_award, per_kg_reconciles, b_quotes_27, e_reference_from_own_records,
       gating_as_designed]


def check_all(truth: dict) -> list[tuple[str, bool, str]]:
    return [(f.__name__, *f(truth)) for f in ALL]
