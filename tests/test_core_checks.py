from datetime import date
from decimal import Decimal as D

from level.core.checks import (
    check_line_amount,
    check_plausibility,
    check_total,
    compare_spec,
    coverage,
)
from level.core.gating import parse_date, vendor_gating, verdict
from level.types import CellState


def test_arithmetic_checks():
    assert check_line_amount(D(100), D("12.5"), D(1250)).passed
    bad = check_line_amount(D(100), D("12.5"), D(1350))
    assert not bad.passed and "1,250.00" in bad.detail
    assert check_total([D(100), D(200)], D(300)).passed
    assert not check_total([D(100), D(200)], D(330)).passed


def test_plausibility_detects_unit_mismatch():
    r = check_plausibility(D(2500), [D(24), D(26), D(25)], None)
    assert not r.passed and r.unit_mismatch == 100
    r = check_plausibility(D("0.025"), [D(24), D(26)], None)
    assert not r.passed and r.unit_mismatch == 1000
    assert check_plausibility(D(26), [D(24), D(25)], D(25)).passed
    r = check_plausibility(D(90), [], D(25))
    assert not r.passed and r.reference_kind == "last year" and r.unit_mismatch is None
    assert check_plausibility(D(5), [], None).passed  # nothing to compare with


def test_spec_comparison_any_attributes():
    rfx = {"ply": 5, "dimensions_mm": "400x300x250", "grade": "150/120/150 BF18"}
    assert compare_spec(rfx, {"ply": "5", "dimensions_mm": "400 x 300 x 250 mm"}) == []
    dev = compare_spec(rfx, {"Grade": "120/100/120 BF16"})
    assert len(dev) == 1 and dev[0].attribute == "grade"
    laptop = {"ram_gb": 16, "cpu": "i7"}
    assert compare_spec(laptop, {"ram_gb": "8"})[0].quoted_value == "8"


def test_coverage_every_vendor_line_has_one_state():
    cells = {(1, 10): CellState.CONFIRMED, (1, 11): CellState.NOT_QUOTED}
    r = coverage([1], [10, 11], cells)
    assert r["complete"]
    assert not coverage([1, 2], [10, 11], cells)["complete"]


def test_gating_rules():
    assert parse_date("31/03/2026") == date(2026, 3, 31)
    assert parse_date("valid till 15 Jan 2025") == date(2025, 1, 15)
    assert verdict("Yes", {"op": "==", "value": "yes"})[0] == "pass"
    assert verdict("No", {"op": "==", "value": "yes"})[0] == "fail"
    assert verdict(None, {"op": "==", "value": "yes"})[0] == "unclear"
    assert verdict("7 days", {"op": "<=", "value": "10"})[0] == "pass"
    assert verdict("15 days", {"op": "<=", "value": "10"})[0] == "fail"
    assert verdict("fast", {"op": "<=", "value": "10"})[0] == "unclear"
    rule = {"op": "valid_on", "value": "2026-04-01"}
    # The attached certificate's date beats a confident "yes".
    v, why = verdict("Yes, certified", rule, expiry=date(2025, 12, 31))
    assert v == "fail" and "2025-12-31" in why
    assert verdict("Yes", rule)[0] == "unclear"
    assert verdict("valid to 2027-01-01", rule)[0] == "pass"
    assert vendor_gating(["pass", "unclear"]) == "unclear"
    assert vendor_gating(["pass", "fail"]) == "fail"


def test_free_delivery_within_a_distance_is_conditional_so_unknown():
    from level.core.landed import freight_for_line, landed_price, parse_freight

    # Code cannot know how far a plant is, so "free within N km" says nothing certain.
    f = parse_freight("Laptops shipped free of charge within 40 km of our depot.", "40 km")
    assert f.kind == "unknown" and "40 km" in f.reason
    f = freight_for_line("Carriage free inside a 25 mile radius.", None, "Lyon", ["Lyon", "Oslo"])
    assert f.kind == "unknown" and "25 mile radius" in f.reason
    land = landed_price(D("900"), freight=f, canonical_uom="unit", conversion_factors={},
                        fx_rates={}, payment_days=None, requested_days=D(45),
                        cost_of_capital=D(0))
    assert land.value is None and "25 mile radius" in land.unknown_reason
    # A distance condition around the line's own place does cover that place.
    f = freight_for_line("Free delivery within 30 km of Lyon.", None, "Lyon", ["Lyon", "Oslo"])
    assert f.kind == "included"
    assert parse_freight("Freight EUR 3.20 per unit", "EUR 3.20").kind == "per_unit"


def test_evidence_check_ignores_table_cell_layout():
    from level.pipeline.intake import Block
    from level.pipeline.verify import check_evidence

    page = Block(unit="page", idx=1, label="page 1",
                 text="[page 1]\nSr\nItem\nRate\n1\nSafety helmet, white\n2,000\n210.00\n")
    res = check_evidence("210.00", "1 | Safety helmet, white | 2,000 | 210.00", "page 1", [page])
    assert res.ok
    assert not check_evidence("215.00", "1 | Safety helmet | 215.00", "page 1", [page]).ok


def test_generic_count_word_on_an_item_unit_line_is_inferred_one_to_one():
    from decimal import Decimal

    from level.core.normalise import normalise_price
    from level.types import Basis

    n = normalise_price("3620", raw_currency="INR", basis=Basis(per="nos", multiple="1000"),
                        uom_text="per 1000 nos", canonical_uom="set", conversion_factors={},
                        fx_rates={})
    assert n.value == Decimal("3.62") and n.assumptions["unit"] == "1 each = 1 set"
    pair = normalise_price("4", raw_currency="INR", basis=Basis(per="piece", multiple=None),
                           uom_text="per piece", canonical_uom="pair", conversion_factors={},
                           fx_rates={})
    assert pair.value is None and pair.error  # a pair is two pieces: never assumed


def test_freight_term_by_delivery_place():
    from level.core.landed import freight_for_place, parse_freight

    places = ["Lyon", "Oslo", "Turin"]
    text = "Courier to Lyon and Turin about EUR 4.50 per laptop; Oslo billed at cost."
    assert parse_freight(freight_for_place(text, "Oslo", places)).kind == "unknown"
    lyon = parse_freight(freight_for_place(text, "Lyon", places))
    assert lyon.kind == "per_unit" and str(lyon.amount) == "4.50"
    assert freight_for_place("Freight included.", "Lyon", places) == "Freight included."


def test_free_delivery_to_a_named_place_covers_only_that_place():
    from level.core.landed import freight_for_line

    places = ["Lyon", "Oslo"]
    term = "Free delivery to Lyon."
    assert freight_for_line(term, None, "Lyon", places).kind == "included"
    elsewhere = freight_for_line(term, None, "Oslo", places)
    assert elsewhere.kind == "unknown"
    assert "Oslo" in elsewhere.reason and term in elsewhere.reason


def test_unreadable_price_is_needs_review_not_not_quoted():
    from decimal import Decimal as D

    from level.core.cells import ClaimInfo, LineInfo, Snapshot, VendorInfo, compute_cells
    from level.types import Basis, CellState, Flag

    line = LineInfo(id=1, line_no=1, description="Item", canonical_uom="each",
                    annual_qty=D(10), factors={}, spec={}, last_price=None)
    claim = ClaimInfo(id=5, vendor_id=1, line_ids=[1], scope="line", item_text="item",
                      raw_price=None, raw_currency="INR", uom_text=None,
                      basis=Basis(per="each", multiple=None), quantity_break=None,
                      evidence_locator="image", evidence_quote="Rs 4?.50", evidence_ok=None,
                      recency="2026-01-01")
    flag = Flag(id=9, rfx_id=1, vendor_id=1, target_kind="claim", target_id=5,
                type="read_mismatch", severity="warning", reason="digit unclear",
                candidates=["Rs. 4?.50"])
    snap = Snapshot(rfx_id=1, base_currency="INR", lines=[line],
                    vendors=[VendorInfo(id=1, name="V", status="replied")], claims=[claim],
                    terms=[], assumptions={}, history={}, flags=[flag], gating={},
                    requested_payment_days=D(60))
    cell = compute_cells(snap)[0]
    assert cell.state == CellState.NEEDS_REVIEW and "4?.50" in cell.reason


def test_a_price_with_an_unreadable_digit_is_not_a_number():
    from decimal import Decimal

    from level.core.normalise import parse_number

    assert parse_number("Rs. 4?.50") is None and parse_number("?2.50") is None
    assert parse_number("Rs. 42.50?") is None
    assert parse_number("Rs. 42.50 (approx.)") == Decimal("42.50")


def test_a_list_number_is_not_a_freight_charge():
    from level.core.landed import parse_freight

    assert parse_freight("1. Prices are inclusive of door delivery.").kind == "included"
    assert parse_freight("2) Courier EUR 2.40 per unit extra").amount is not None


def test_yes_no_after_a_label():
    from level.core.gating import parse_yes_no

    assert parse_yes_no("Licensed reseller: yes (letter enclosed)") is True
    assert parse_yes_no("On-site support: No") is False
    assert parse_yes_no("Our reseller status dates from 2019") is None  # not a label
    assert parse_yes_no("Note: see attached") is None


def test_prose_yes_no_answer_uses_the_readers_stance():
    """H1: 'We test every batch in our own lab' is a yes, whether or not the word is there."""
    from level.core.gating import verdict
    rule = {"op": "==", "value": "yes"}
    prose = "Our own laboratory tests every batch before dispatch."
    assert verdict(prose, rule)[0] == "unclear"  # no stance: the text alone is not enough
    v, why = verdict(prose, rule, stance="yes")
    assert v == "pass" and "reads as yes" in why
    assert verdict(prose, rule, stance="no")[0] == "fail"
    assert verdict("No.", rule, stance="yes")[0] == "fail"  # a plain word beats the reading
