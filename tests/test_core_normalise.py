import random
from decimal import Decimal as D

from level.core.landed import landed_price, parse_days, parse_freight
from level.core.normalise import (
    currency_code,
    is_tax_inclusive,
    normalise_price,
    parse_number,
    parse_percent,
    quoted_unit,
    select_slab,
    unit_key,
)
from level.types import Basis, QuantityBreak

FX = {"USD": D("83")}


def norm(price, per=None, multiple=None, uom=None, canonical="piece", factors=None, **kw):
    basis = Basis(per=per, multiple=multiple) if per else None
    return normalise_price(price, raw_currency=kw.pop("currency", "INR"), basis=basis,
                           uom_text=uom, canonical_uom=canonical,
                           conversion_factors=factors or {}, fx_rates=FX, **kw)


def test_parse_number_as_written():
    assert parse_number("₹1,250.00/-") == D("1250.00")
    assert parse_number("Rs. 42") == D("42")
    assert parse_number("1,20,000") == D("120000")
    assert parse_number("1.5 lakh") == D("150000")
    assert parse_number("50k") == D("50000")
    assert parse_number("n/a") is None
    assert parse_percent("2% discount") == D("0.02")
    assert parse_percent("two") is None


def test_currency_and_units():
    assert currency_code("Rs.") == "INR"
    assert currency_code("US$") == "USD"
    assert currency_code("₹ 42") == "INR"
    assert unit_key("Pcs.") == "each" and unit_key("Nos") == "each"
    assert unit_key("Kgs") == "kg" and unit_key("boxes") == "box"
    assert quoted_unit(Basis(per="piece", multiple="1000"), None) == ("each", D(1000))
    assert quoted_unit(None, "per 100") == ("each", D(100))
    assert quoted_unit(None, "per thousand") == ("each", D(1000))


def test_per_thousand_pieces():
    r = norm("12,500", per="piece", multiple="1000")
    assert r.value == D("12.5")
    assert r.steps[0].operation == "divide by 1000"
    assert r.assumptions == {}  # pure arithmetic, nothing assumed


def test_per_kg_uses_line_weight():
    r = norm("42", per="kg", factors={"g_per_piece": "412"})
    assert r.value.quantize(D("0.0001")) == D("17.3040")
    assert "factor.g->each" in r.assumptions  # the weight was used: cell will be Inferred


def test_per_kg_without_weight_cannot_convert():
    r = norm("42", per="kg")
    assert r.value is None and "cannot convert" in r.error


def test_unknown_unit_is_an_explicit_assumption():
    r = norm("2500", per="box")
    assert r.value == D("2500")
    assert r.assumptions["unit"] == "1 box = 1 piece"


def test_usd_to_inr_and_gst_and_discount():
    r = norm("0.40", per="piece", currency="USD")
    assert r.value == D("33.20") and r.assumptions["fx.USDINR"] == "83"
    r = norm("118", per="piece", tax_inclusive=True, tax_rate=D("0.18"))
    assert r.value == D("100")
    r = norm("100", per="piece", discounts=[(D("0.02"), "2%")])
    assert r.value == D("98.00")


def test_missing_price_is_not_zero():
    r = norm(None, per="piece")
    assert r.value is None and r.error == "no price stated"


def test_non_packaging_each_and_dozen():
    """A three-line RFx for another category: units 'each' and 'per dozen'."""
    assert norm("120", per="dozen", canonical="each").value == D("10")
    assert norm("2", per="each", canonical="dozen").value == D("24")
    assert norm("30", per="pack", canonical="each", factors={"each_per_pack": "6"}).value == D(5)


def test_per_kg_round_trip_property():
    rng = random.Random(7)
    for _ in range(300):
        grams = D(rng.randint(50, 5000)) / D(rng.choice([1, 10]))
        price = D(rng.randint(100, 99999)) / D(100)
        factors = {"g_per_piece": str(grams)}
        per_piece = norm(str(price), per="kg", factors=factors).value
        back = norm(str(per_piece), per="piece", canonical="kg", factors=factors).value
        assert back.quantize(D("0.000001")) == price.quantize(D("0.000001"))


def test_tax_inclusive_detection():
    assert is_tax_inclusive("Rate per pc (incl. GST)")
    assert is_tax_inclusive("prices inclusive of GST")
    assert not is_tax_inclusive("Rate per pc, GST extra", "plus GST")
    assert not is_tax_inclusive("Rate per piece")


def test_slab_selection_by_annual_qty():
    slabs = [QuantityBreak(text="up to 50,000", min_qty=None, max_qty="50,000"),
             QuantityBreak(text="50,001 - 2,00,000", min_qty="50,001", max_qty="2,00,000"),
             QuantityBreak(text="above 2,00,000", min_qty=None, max_qty=None)]
    assert select_slab(slabs, D(30000)) == 0
    assert select_slab(slabs, D(120000)) == 1
    assert select_slab(slabs, D(600000)) == 2


def test_freight_parsing():
    assert parse_freight("Shipping billed extra").kind == "unknown"
    assert parse_freight("Freight included").kind == "included"
    f = parse_freight("Freight Rs 1.50 per piece", "Rs 1.50")
    assert (f.kind, f.amount, f.unit) == ("per_unit", D("1.50"), "each")
    assert parse_freight("3% of invoice value").kind == "percent"
    assert parse_freight("Rs 4500 per truck").kind == "per_trip"
    assert parse_days("90 days from invoice") == D(90)
    assert parse_days("100% advance") == D(0)


def _landed(freight, days, **kw):
    return landed_price(D("100"), freight=freight, canonical_uom="piece",
                        conversion_factors=kw.get("factors", {"g_per_piece": "500"}),
                        fx_rates=FX, payment_days=days, requested_days=D(60),
                        cost_of_capital=D("0.12"))


def test_landed_paths():
    assert _landed(parse_freight("included"), D(60)).value == D(100)
    assert _landed(parse_freight("Rs 2 per kg", "Rs 2"), D(60)).value == D(101)  # 0.5 kg
    assert _landed(parse_freight("5% of value"), D(60)).value == D(105)
    unknown = _landed(parse_freight("shipping billed extra"), D(60))
    assert unknown.value is None and "not stated" in unknown.unknown_reason
    # 30-day terms vs 60 requested: 100 x 0.12 x 30/365 more.
    early = _landed(parse_freight("included"), D(30)).value
    assert early.quantize(D("0.0001")) == D("100.9863")
    assert _landed(parse_freight("Rs 4500 per trip"), D(60)).value is None


def test_pack_of_n_is_divided_once():
    """H3: 'Rs 196 / box of 50 pair' on a line with 50 pairs per box is 3.92 a pair."""
    from level.core.normalise import quoted_unit
    from level.types import Basis
    assert quoted_unit(Basis(per="box", multiple="50"), "box of 50 pair") == ("pair", 50)
    assert quoted_unit(Basis(per="box", multiple="50"), "box of 50") == (None, 50)
    assert quoted_unit(None, "per dozen sets") == ("set", 12)
    assert quoted_unit(None, "ex-GST") == (None, 1)
