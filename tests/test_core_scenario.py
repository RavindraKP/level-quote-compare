"""Cells and scenarios on a small hand-built event, with totals worked out by hand.

Lines (qty, last year's price): L1 100 @10, L2 200 @20, L3 300 @30.
Prices per piece, INR, freight included, 60-day terms (so landed = ex-works):
    V1: 10, 19, 31      V2: --, 21, 28      V3: 8, 18, 27 (fails gating)
    V5: 9.5, 25, 29     V4: invited, no reply
"""

from decimal import Decimal as D

from level.core.cells import (
    ClaimInfo,
    LineInfo,
    Snapshot,
    TermInfo,
    VendorInfo,
    compute_cells,
    own_record,
)
from level.core.scenario import solve
from level.types import Basis, CellState, Flag, ScenarioParams

PRICES = {1: ["10", "19", "31"], 2: [None, "21", "28"], 3: ["8", "18", "27"],
          5: ["9.5", "25", "29"]}


def claim(cid, vendor, line, price, scope="line", per="piece", currency="INR", ok=True):
    return ClaimInfo(id=cid, vendor_id=vendor, line_ids=[line], scope=scope, item_text="item",
                     raw_price=price, raw_currency=currency, uom_text=None,
                     basis=Basis(per=per, multiple=None), quantity_break=None,
                     evidence_locator="page 1", evidence_quote=str(price), evidence_ok=ok,
                     recency="2026-01-01")


def term(tid, vendor, type_, text, value=None, conditional=False):
    return TermInfo(id=tid, vendor_id=vendor, type=type_, text=text, value=value,
                    conditions=None, is_conditional=conditional, evidence_locator="page 1",
                    recency="2026-01-01")


def snapshot(**overrides) -> Snapshot:
    lines = [LineInfo(id=i, line_no=i, description=f"Item {i}", canonical_uom="piece",
                      annual_qty=D(q), factors={"g_per_piece": "500"}, spec={},
                      last_price=D(lp))
             for i, q, lp in [(1, 100, 10), (2, 200, 20), (3, 300, 30)]]
    vendors = [VendorInfo(id=v, name=f"V{v}", status="replied") for v in (1, 2, 3, 5)]
    vendors.append(VendorInfo(id=4, name="V4", status="invited"))
    claims, cid = [], 100
    for v, prices in PRICES.items():
        for line, p in enumerate(prices, start=1):
            if p is not None:
                cid += 1
                claims.append(claim(cid, v, line, p))
    terms = [term(200 + v, v, "freight", "Freight included") for v in PRICES]
    snap = Snapshot(rfx_id=1, base_currency="INR", requested_payment_days=D(60), lines=lines,
                    vendors=vendors, claims=claims, terms=terms, flags=[],
                    assumptions={"fx.USDINR": "83", "gst_rate": "0.18",
                                 "cost_of_capital": "0.12"},
                    gating={1: ["pass"], 2: ["pass"], 3: ["fail"], 4: ["unclear"],
                            5: ["pass"]})
    for k, v in overrides.items():
        setattr(snap, k, v)
    return snap


def cell(cells, vendor, line):
    return next(c for c in cells if c.vendor_id == vendor and c.rfx_line_id == line)


def test_states_not_quoted_and_awaiting():
    cells = compute_cells(snapshot())
    assert len(cells) == 5 * 3  # every vendor x line has exactly one cell
    nq = cell(cells, 2, 1)
    assert nq.state == CellState.NOT_QUOTED and nq.unit_price_inr is None  # never priced
    aw = cell(cells, 4, 2)
    assert aw.state == CellState.AWAITING and aw.unit_price_inr is None
    ok = cell(cells, 1, 2)
    assert ok.state == CellState.CONFIRMED and ok.unit_price_inr == D("19")
    assert ok.landed_price_inr == D("19") and ok.confirmed_by == "checks"


def test_flags_and_evidence_drive_state():
    snap = snapshot()
    snap.claims[0].evidence_ok = False  # V1 line 1
    snap.flags = [Flag(id=1, rfx_id=1, vendor_id=1, target_kind="claim", target_id=102,
                       rfx_line_id=2, type="spec_deviation", severity="critical",
                       reason="lighter board")]
    cells = compute_cells(snap)
    assert cell(cells, 1, 1).state == CellState.NEEDS_REVIEW
    assert cell(cells, 1, 2).state == CellState.CONFLICT
    snap.flags[0].state, snap.flags[0].closed_by = "closed", "buyer"
    c = cell(compute_cells(snap), 1, 2)
    assert c.state == CellState.CONFIRMED and c.confirmed_by == "buyer"


def test_inferred_from_weight_and_reference_history():
    snap = snapshot()
    snap.claims[0] = claim(101, 1, 1, "40", per="kg")  # 40/kg x 0.5 kg = 20
    snap.claims.append(claim(999, 2, 1, None, scope="reference"))
    snap.history = {(2, 1): {"unit_price": "11", "currency": "INR", "uom": "piece",
                             "period": "FY25", "source": "contract"}}
    cells = compute_cells(snap)
    kg = cell(cells, 1, 1)
    assert kg.state == CellState.INFERRED and kg.unit_price_inr == D("20")
    ref = cell(cells, 2, 1)
    assert ref.state == CellState.INFERRED and ref.unit_price_inr == D("11")
    assert "history.FY25" in ref.assumptions
    snap.history = {}  # vendor has no record for that line: unresolved, never borrowed
    assert cell(compute_cells(snap), 2, 1).state == CellState.NOT_QUOTED


def test_own_record_prefers_contract_then_falls_back_to_quote():
    quote = {"unit_price": "12", "source": "quote"}
    contract = {"unit_price": "11", "source": "contract"}
    assert own_record([contract, quote])["unit_price"] == "11"
    assert own_record([quote, contract])["unit_price"] == "11"
    assert own_record([quote])["unit_price"] == "12"  # quoted last year without winning
    snap = snapshot()
    snap.claims.append(claim(999, 2, 1, None, scope="reference"))
    snap.history = {(2, 1): {**quote, "currency": "INR", "uom": "piece", "period": "FY25"}}
    ref = cell(compute_cells(snap), 2, 1)
    assert ref.state == CellState.INFERRED and ref.unit_price_inr == D("12")
    assert "quote" in ref.assumptions["history.FY25"]


def test_savings_baseline_is_last_years_contract_never_a_losing_quote():
    snap = snapshot()
    before = solve(snap, ScenarioParams(strategy="cheapest_per_line", require_gating_pass=False))
    # A cheaper losing quote in the history changes nothing: the baseline is what was paid.
    snap.history = {(v.id, ln.id): {"unit_price": "0.01", "source": "quote", "currency": "INR",
                                    "uom": "piece", "period": "FY25"}
                    for v in snap.vendors for ln in snap.lines}
    after = solve(snap, ScenarioParams(strategy="cheapest_per_line", require_gating_pass=False))
    assert after.last_year_total_inr == before.last_year_total_inr
    assert after.savings_inr == before.savings_inr
    expected = sum((ln.last_price * ln.annual_qty for ln in snap.lines
                    if ln.last_price is not None), D(0))
    assert before.last_year_total_inr == expected


def test_usd_conditional_discount_and_freight_unknown():
    snap = snapshot()
    snap.claims[0] = claim(101, 1, 1, "0.10", currency="USD")
    snap.terms = [term(1, 1, "discount", "2% if volume above 1 lakh", "2%", conditional=True),
                  term(2, 1, "freight", "Shipping billed extra")]
    c = cell(compute_cells(snap), 1, 1)
    assert c.unit_price_inr == D("8.3") and c.landed_price_inr is None
    assert c.state == CellState.INFERRED  # FX is an assumption
    c2 = cell(compute_cells(snap, apply_conditional_discounts=True,
                            fx_override={"USDINR": D(90)}), 1, 1)
    assert c2.unit_price_inr == D("8.82")  # 0.10 x 90 x 0.98


def test_single_vendor_auto_and_named():
    r = solve(snapshot(), ScenarioParams(strategy="single_vendor"))
    assert r.vendors_used == [1] and r.total_inr == D(14100)  # 1000 + 3800 + 9300
    r = solve(snapshot(), ScenarioParams(strategy="single_vendor", single_vendor_id=2))
    assert r.total_inr == D(12600) and r.coverage_gaps == [1]  # gap stated, not zero-filled
    assert any("no eligible price" in c for c in r.caveats)


def test_cheapest_per_line_without_and_with_gating():
    r = solve(snapshot(), ScenarioParams(strategy="cheapest_per_line", require_gating_pass=False))
    assert r.total_inr == D(12500) and r.vendors_used == [3]
    r = solve(snapshot(), ScenarioParams(strategy="cheapest_per_line"))
    # L1 V5 9.5x100=950, L2 V1 19x200=3800, L3 V2 28x300=8400
    assert r.total_inr == D(13150) and r.vendors_used == [1, 2, 5]
    assert r.last_year_total_inr == D(14000) and r.savings_inr == D(850)
    assert any("V3 excluded" in c for c in r.caveats)
    assert any("V4 has not replied" in c for c in r.caveats)


def test_cap_of_two_vendors():
    r = solve(snapshot(), ScenarioParams(strategy="capped_vendors", max_vendors=2))
    # {V1,V2}: 1000 + 3800 + 8400 = 13200; {V1,V5}: 13450; {V2,V5}: 13550
    assert r.total_inr == D(13200) and r.vendors_used == [1, 2]


def test_unconfirmed_treatment_and_sensitivity():
    snap = snapshot()
    snap.claims = [c if not (c.vendor_id == 1 and c.line_ids == [2])
                   else claim(c.id, 1, 2, "19", ok=False) for c in snap.claims]
    params = dict(strategy="cheapest_per_line")
    r = solve(snap, ScenarioParams(**params))
    item = next(s for s in r.sensitivity if s.rfx_line_id == 2)
    # 19 + 10% = 20.9, still below the next price (V2, 21): the winner holds
    assert item.vendor_id == 1 and item.winner_changes is False
    assert item.exposure_inr == D("380.0")  # 19 x 10% x 200
    r = solve(snap, ScenarioParams(**params, unconfirmed="exclude"))
    assert next(a for a in r.allocation if a.rfx_line_id == 2).vendor_id == 2
    r = solve(snap, ScenarioParams(**params, unconfirmed="pessimistic"))
    assert next(a for a in r.allocation if a.rfx_line_id == 2).vendor_id == 1  # 20.9 < 21


def test_landed_basis_excludes_unknown_freight_with_caveat():
    snap = snapshot()
    snap.terms = [t for t in snap.terms if t.vendor_id != 3] + [term(9, 3, "freight", "extra")]
    r = solve(snap, ScenarioParams(strategy="cheapest_per_line", require_gating_pass=False))
    assert 3 not in r.vendors_used
    assert any("delivered price unknown" in c for c in r.caveats)
    r = solve(snap, ScenarioParams(strategy="cheapest_per_line", require_gating_pass=False,
                                   price_basis="ex_works"))
    assert r.vendors_used == [3]


def test_passed_only_excludes_unclear_gating():
    snap = snapshot()
    snap.gating[5] = ["pass", "unclear"]  # V5's answers are unclear
    keep = solve(snap, ScenarioParams(strategy="cheapest_per_line", price_basis="ex_works"))
    drop = solve(snap, ScenarioParams(strategy="cheapest_per_line", price_basis="ex_works",
                                      exclude_unclear_gating=True))
    assert 5 in keep.vendors_used and 5 not in drop.vendors_used
    assert any("V5 excluded" in c for c in drop.caveats)


def test_no_price_on_basis_is_no_total_never_zero():
    """B5: a single-vendor option whose landed prices are all unknown has no total."""
    from level.core import summary
    snap = snapshot()
    snap.terms = [t for t in snap.terms if t.vendor_id != 1] + [term(9, 1, "freight", "extra")]
    none = solve(snap, ScenarioParams(strategy="single_vendor", single_vendor_id=1,
                                      require_gating_pass=False))
    assert none.total_inr is None and none.vendors_used == []
    assert any("no total" in c for c in none.caveats)
    some = solve(snap, ScenarioParams(strategy="single_vendor", single_vendor_id=2,
                                      require_gating_pass=False))
    rows = summary.compare([some, none])
    assert rows[1]["total_inr"] is None and rows[1]["total_minus_first_inr"] is None
    assert summary.compare([none, some])[1]["pct_vs_first"] is None


def test_as_quoted_and_term_text_do_not_repeat_what_the_vendor_wrote():
    """M10/M11: no 'Rs Rs. 40.50 / / kg', no date written twice in a caveat."""
    from level.core.cells import _as_quoted
    from level.core.scenario import term_text

    def c(price, currency, uom, per=None):
        base = claim(1, 1, 1, price, per=per or "piece", currency=currency)
        base.uom_text = uom
        return base

    assert _as_quoted(c("Rs. 40.50", "Rs", "/ kg", "kg")) == "Rs. 40.50 / kg"
    assert _as_quoted(c("20400", "Rs.", "Rate / 1000 Nos")) == "Rs. 20400 / 1000 Nos"
    assert _as_quoted(c("58,900", "Rs", "ex-GST", "set")) == "Rs 58,900 / set (ex-GST)"
    t = term(1, 1, "validity", "4. Rates valid till 31.03.2026.", "31.03.2026")
    assert term_text(t) == "Rates valid till 31.03.2026."


def test_likely_unit_slip_is_left_out_of_totals():
    """H4: a price about 100x its peers is shown but never summed at face value."""
    from level.core.cells import in_totals
    snap = snapshot()
    snap.flags = [Flag(id=1, rfx_id=1, vendor_id=1, target_kind="cell", target_id=None,
                       rfx_line_id=3, type="plausibility", severity="warning",
                       reason="about 100x higher than the peer median (28.00): likely a "
                              "unit-basis mismatch (e.g. per 100 vs per piece)")]
    c = cell(compute_cells(snap), 1, 3)
    assert c.state == CellState.NEEDS_REVIEW and not in_totals(c)
    r = solve(snap, ScenarioParams(strategy="single_vendor", single_vendor_id=1))
    assert r.total_inr == D(4800)  # lines 1 and 2 only: 1000 + 3800, line 3 left out
    assert any("different unit" in x for x in r.caveats)
