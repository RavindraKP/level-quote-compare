"""Build the ground truth for one seed: lines, history, five vendors, answers, traps.

Everything a document shows is decided here first; the renderers only lay it out.
All money is Decimal and stored as strings. Prices are INR, ex-GST, per the line's
canonical unit, before conditional discounts (the "normalised" value the grid should show).

The designed outcomes in docs/DATASET.md are built in by construction (C is priced off A and
B, D off a cheaper board rate, ...) and asserted in tests/test_dataset.py.
"""

from __future__ import annotations

import math
import random
from decimal import ROUND_HALF_UP, Decimal

# --- Constants of the fictional event ---------------------------------------------------

BUYER = {
    "name": "Sundari Foods Pvt Ltd",
    "address": "Survey No. 112, Hinjewadi Phase 2, Pune 411057",
    "contact": "Anita Kulkarni, Category Manager — Packaging",
    "email": "purchase@sundarifoods.example",
    "plants": ["Pune", "Hosur", "Baddi"],
}

RFX = {
    "ref": "SF/PKG/FY26/014",
    "title": "Annual rate contract — corrugated shipper cartons, FY26",
    "category": "Corrugated packaging",
    "issued": "2025-02-03",
    "due": "2025-02-24",
    "award_date": "2025-03-14",
    "basis": "Price per piece, INR, ex-GST, delivered to plant, 60-day payment",
    "payment_days_requested": 60,
}

# FX: the buyer's reference rate on the reply date (fictional dataset; not a published RBI figure).
ASSUMPTIONS = {
    "fx.USDINR": {"value": "86.70", "source": "Finance team reference rate, 24 Feb 2025",
                  "as_of": "2025-02-24"},
    "cost_of_capital": {"value": "0.10", "source": "Finance team, annual rate",
                        "as_of": "2025-02-01"},
    "gst_rate": {"value": "0.18", "source": "GST on corrugated boxes (HSN 4819)",
                 "as_of": "2025-02-01"},
}

VENDORS = {
    "A": {"name": "Kaveri Packaging", "email": "sales@kaveripackaging.example",
          "contact": "R. Venkatesh", "city": "Hosur"},
    "B": {"name": "Shreeji Cartons", "email": "quotes@shreejicartons.example",
          "contact": "Mitesh Shah", "city": "Pune"},
    "C": {"name": "Pacific Board Industries", "email": "exports@pacificboard.example",
          "contact": "Farhan Qureshi", "city": "Mundra"},
    "D": {"name": "Bharat Corrugators", "email": "bharatcorrugators@mail.example",
          "contact": "Santosh Jadhav", "city": "Chakan, Pune"},
    "E": {"name": "Mahalaxmi Packaging", "email": "suresh.mahalaxmi@mail.example",
          "contact": "Suresh Patil", "city": "Bhosari, Pune"},
}

# Board build per ply: liner, medium and flute papers in GSM. Flutes take up ~45% extra paper.
PLY_BOARD = {
    3: {"liners": [150, 150], "mediums": [], "flutes": [120], "bf": 18, "flute": "B",
        "grade": "150/120/150"},
    5: {"liners": [150, 150], "mediums": [120], "flutes": [120, 120], "bf": 20, "flute": "BC",
        "grade": "150/120/120/120/150"},
    7: {"liners": [180, 180], "mediums": [120, 120], "flutes": [120, 120, 120], "bf": 22,
        "flute": "BCB", "grade": "180/120/120/120/120/120/180"},
}
FLUTE_TAKE_UP = Decimal("1.45")

# A "fair" market price per line: board cost per kg + conversion + print. Placeholders.
FAIR_RATE_PER_KG = {3: Decimal("40"), 5: Decimal("43"), 7: Decimal("45")}
CONVERSION_PER_PIECE = Decimal("0.80")
PRINT_ADDER = {"plain": Decimal("0"), "1-colour": Decimal("0.60"), "2-colour": Decimal("1.10")}

# How many lines of each kind, and how they spread over the plants.
PLANT_MIX = {
    "3ply": ["Pune"] * 5 + ["Hosur"] * 4 + ["Baddi"] * 3,
    "5ply": ["Pune"] * 6 + ["Hosur"] * 4 + ["Baddi"] * 3,
    "7ply": ["Pune", "Hosur", "Baddi"],
    "partition": ["Pune"],
    "pad": ["Hosur"],
}

SLAB_THRESHOLD = 100_000  # vendor A's slab break, pieces per year
GATING_QUESTIONS = (1, 2, 8)


# --- Small helpers --------------------------------------------------------------------


def dec(x) -> Decimal:
    return Decimal(str(x))


def q(x: Decimal, places: str = "0.01") -> Decimal:
    """Round half-up to the given places, e.g. q(x, '0.0001')."""
    return x.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def s(x: Decimal | None) -> str | None:
    """Decimal -> plain string for JSON, with no exponent and no trailing zeros noise."""
    return None if x is None else format(x.normalize(), "f")


def u(rng: random.Random, lo: float, hi: float) -> Decimal:
    """Uniform random Decimal with 4 places."""
    return q(dec(rng.uniform(lo, hi)), "0.0001")


def round_to(rng_value: float, step: int) -> int:
    return int(round(rng_value / step) * step)


# --- Lines ----------------------------------------------------------------------------


def board_gsm(ply: int) -> Decimal:
    b = PLY_BOARD[ply]
    return dec(sum(b["liners"]) + sum(b["mediums"])) + FLUTE_TAKE_UP * sum(b["flutes"])


def rsc_weight_g(ply: int, length: int, width: int, height: int) -> int:
    """Regular slotted carton: blank = (2(L+W) + 35 mm glue flap) x (W + H). Grams, rounded."""
    area_m2 = dec((2 * (length + width) + 35) * (width + height)) / dec(1_000_000)
    return int(q(area_m2 * board_gsm(ply), "1"))


def _dims(rng: random.Random, lo: int, hi: int) -> tuple[int, int, int]:
    length = round_to(rng.uniform(lo, hi), 10)
    width = round_to(length * rng.uniform(0.55, 0.8), 10)
    height = round_to(width * rng.uniform(0.6, 1.0), 10)
    return length, width, height


def fair_price(line: dict) -> Decimal:
    """What the line 'should' cost in this market; vendors are priced around it."""
    kg = dec(line["ref_weight_g"]) / 1000
    if line["kind"] == "partition":
        return q(kg * FAIR_RATE_PER_KG[3] + Decimal("0.60"), "0.0001")  # slotting and assembly
    if line["kind"] == "pad":
        return q(kg * FAIR_RATE_PER_KG[3] + Decimal("0.35"), "0.0001")
    ply = line["spec"]["ply"]
    return q(kg * FAIR_RATE_PER_KG[ply] + CONVERSION_PER_PIECE + PRINT_ADDER[line["spec"]["print"]],
             "0.0001")


def _carton_line(rng: random.Random, kind: str, plant: str) -> dict:
    ply = int(kind[0])
    lo, hi = {3: (220, 420), 5: (380, 560), 7: (480, 640)}[ply]
    length, width, height = _dims(rng, lo, hi)
    pr = rng.choices(["plain", "1-colour", "2-colour"], weights=[4, 4, 2])[0]
    weight = rsc_weight_g(ply, length, width, height)
    b = PLY_BOARD[ply]
    print_text = "plain" if pr == "plain" else f"{pr} print"
    return {
        "kind": kind,
        "description": f"{ply}-ply RSC shipper {length} x {width} x {height} mm, {print_text}",
        "spec": {"style": "RSC", "ply": ply, "length_mm": length, "width_mm": width,
                 "height_mm": height, "board_gsm": b["grade"], "bursting_factor_bf": b["bf"],
                 "flute": b["flute"], "print": pr},
        "canonical_uom": "piece",
        "ref_weight_g": weight,
        "conversion_factors": {"g_per_piece": str(weight)},
        "plant": plant,
    }


def _accessory_line(rng: random.Random, kind: str, plant: str) -> dict:
    if kind == "partition":
        cells = rng.choice([6, 9, 12])
        weight = rng.randint(60, 90)
        return {
            "kind": kind,
            "description": f"Partition set, {cells}-cell, 3-ply board, for glass jar shipper",
            "spec": {"style": "partition", "ply": 3, "cells": cells, "board_gsm": "150/120/150",
                     "print": "plain"},
            "canonical_uom": "set",
            "ref_weight_g": weight,
            "conversion_factors": {"g_per_set": str(weight)},
            "plant": plant,
        }
    length = round_to(rng.uniform(380, 520), 10)
    width = round_to(length * rng.uniform(0.6, 0.8), 10)
    weight = int(q(dec(length * width) / dec(1_000_000) * board_gsm(3), "1"))
    return {
        "kind": kind,
        "description": f"Layer pad, 3-ply, {length} x {width} mm, plain",
        "spec": {"style": "pad", "ply": 3, "length_mm": length, "width_mm": width,
                 "board_gsm": "150/120/150", "print": "plain"},
        "canonical_uom": "piece",
        "ref_weight_g": weight,
        "conversion_factors": {"g_per_piece": str(weight)},
        "plant": plant,
    }


def build_lines(rng: random.Random) -> list[dict]:
    """30 lines in a shuffled order (buyers list by SKU, not by ply)."""
    raw = []
    for kind, plants in PLANT_MIX.items():
        plants = plants[:]
        rng.shuffle(plants)
        for plant in plants:
            if kind in ("partition", "pad"):
                raw.append(_accessory_line(rng, kind, plant))
            else:
                raw.append(_carton_line(rng, kind, plant))
    rng.shuffle(raw)

    cartons = [i for i, ln in enumerate(raw) if ln["kind"] not in ("partition", "pad")]
    big = set(rng.sample(cartons, 3))  # a few large lines dominate spend, as in real life
    skus = rng.sample(range(101, 400), len(raw))
    for i, line in enumerate(raw):
        line["line_no"] = i + 1
        line["sku"] = f"SF-CB-{skus[i]}"
        if i in big:
            qty = round_to(rng.uniform(200_000, 320_000), 10_000)
        elif line["kind"] == "partition":
            qty = round_to(rng.uniform(60_000, 150_000), 5_000)
        elif line["kind"] == "pad":
            qty = round_to(rng.uniform(150_000, 300_000), 5_000)
        else:
            qty = round_to(math.exp(rng.uniform(math.log(15_000), math.log(85_000))), 5_000)
        line["annual_qty"] = qty
        line["fair_price"] = s(fair_price(line))
    return raw


def assign_contracts(rng: random.Random, lines: list[dict]) -> list[dict]:
    """FY25 contract: one awarded vendor per line, at the price the buyer paid.

    E supplied every 7-ply line and both accessories (the lines its email calls "the rest");
    B a few 3-ply lines; A, the incumbent, everything else.
    """
    by_kind = {k: [ln for ln in lines if ln["kind"] == k] for k in PLANT_MIX}
    e_nos = {ln["line_no"] for ln in by_kind["7ply"] + by_kind["partition"] + by_kind["pad"]}
    b_nos = {ln["line_no"] for ln in rng.sample(by_kind["3ply"], 3)}
    history = []
    for line in lines:
        vendor = "E" if line["line_no"] in e_nos else "B" if line["line_no"] in b_nos else "A"
        price = q(dec(line["fair_price"]) * u(rng, 1.01, 1.06))
        line["last_price"] = s(price)  # the savings baseline: contract prices only
        line["last_vendor"] = vendor
        history.append(history_row(line, vendor, price, "contract"))
    return history


def history_row(line: dict, vendor: str, price: Decimal, source: str) -> dict:
    """One FY25 record. source: "contract" (what the buyer paid) or "quote" (a losing bid)."""
    return {"line_no": line["line_no"], "sku": line["sku"], "description": line["description"],
            "vendor": vendor, "source": source, "unit_price": s(price), "currency": "INR",
            "uom": line["canonical_uom"], "period": "FY25"}


def losing_quotes(rng: random.Random, lines: list[dict], contracts: list[dict],
                  bid_lines: dict[str, set[int]]) -> list[dict]:
    """FY25 quotes that did not win: every vendor bid last year, above the price that won."""
    won = {(h["vendor"], h["line_no"]) for h in contracts}
    paid = {h["line_no"]: dec(h["unit_price"]) for h in contracts}
    rows = []
    for vendor, nos in bid_lines.items():
        for line in lines:
            no = line["line_no"]
            if no in nos and (vendor, no) not in won:
                rows.append(history_row(line, vendor, q(paid[no] * u(rng, 1.02, 1.09)), "quote"))
    return rows


def own_record(history: list[dict], vendor: str, line_no: int) -> dict | None:
    """A vendor's own FY25 record for a line: the contract price first, else its FY25 quote."""
    mine = [h for h in history if h["vendor"] == vendor and h["line_no"] == line_no]
    mine.sort(key=lambda h: h["source"] != "contract")
    return mine[0] if mine else None


# --- Vendor cells ---------------------------------------------------------------------


def cell(line: dict, unit: Decimal | None, freight: Decimal | None, state: str,
         as_quoted: str | None, note: str = "") -> dict:
    """One vendor x line truth record. unit None = not quoted; freight None = unknown."""
    unit = None if unit is None else q(unit, "0.0001")
    landed = None if unit is None or freight is None else unit + freight
    return {"line_no": line["line_no"], "quoted": unit is not None,
            "unit_price_inr": s(unit), "freight_inr": s(freight),
            "landed_price_inr": s(landed), "expected_state": state,
            "as_quoted": as_quoted, "note": note}


def vendor_a(rng: random.Random, lines: list[dict]) -> dict:
    """Excel, own codes and order, per 1000 pieces, two slabs, two sheets, a revision."""
    doc_rows, cells = [], {}
    revised = set(ln["line_no"] for ln in rng.sample(
        [ln for ln in lines if ln["kind"] not in ("partition", "pad")], 2))
    revisions, used = [], set()
    for line in lines:
        base = dec(line["fair_price"]) * u(rng, 0.995, 1.03)
        upto = round_to(float(base) * 100, 1) * 10  # per 1000, to the nearest Rs 10
        above = round_to(upto * 0.97, 10)
        slab = "above" if line["annual_qty"] > SLAB_THRESHOLD else "upto"
        rate = above if slab == "above" else upto
        code = a_code(line)
        while code in used:  # two items can share a size; vendor adds a suffix
            code += "A"
        used.add(code)
        doc_rows.append({"line_no": line["line_no"], "code": code, "rate_upto": upto,
                         "rate_above": above, "slab_used": slab})
        note = f"slab {'above' if slab == 'above' else 'up to'} 1,00,000 nos used"
        if line["line_no"] in revised:
            new = round_to(rate * 0.96, 10)
            revisions.append({"line_no": line["line_no"], "code": code, "old_rate": rate,
                              "new_rate": new})
            rate, note = new, f"revised in second email (was Rs {rate:,}/1000)"
        cells[line["line_no"]] = cell(line, dec(rate) / 1000, Decimal("0"), "confirmed",
                                      f"Rs {rate:,} per 1000 nos", note)
    return {"payment_days": 60, "freight": "Included (FOR destination)", "validity_days": 380,
            "cells": cells,
            "doc": {"rows": doc_rows, "revisions": revisions, "quote_no": "KP/Q/24-25/0187",
                    "quote_date": "2025-02-21", "revision_date": "2025-02-26"}}


def a_code(line: dict) -> str:
    sp = line["spec"]
    if line["kind"] == "partition":
        return f"KP-PT-{sp['cells']:02d}"
    if line["kind"] == "pad":
        return f"KP-PD-{sp['length_mm'] // 10:02d}"
    return f"KP-{sp['ply']}P-{sp['length_mm'] // 10:02d}{sp['height_mm'] // 10:02d}"


def vendor_b(rng: random.Random, lines: list[dict]) -> dict:
    """PDF, per piece, declines the 7-ply lines, GST-inclusive page, footnote rebate."""
    gst = dec(ASSUMPTIONS["gst_rate"]["value"])
    page_one = 16  # rows on page 1; the rest are on page 2, where rates include GST
    cells, rows = {}, []
    for line in lines:
        if line["kind"] == "7ply":
            cells[line["line_no"]] = cell(line, None, None, "not_quoted", None,
                                          "Regret, not in our manufacturing range")
            rows.append({"line_no": line["line_no"], "declined": True})
            continue
        factor = u(rng, 0.93, 0.96) if line["kind"] == "3ply" else u(rng, 1.00, 1.04)
        ex_gst = q(dec(line["fair_price"]) * factor)
        incl = line["line_no"] > page_one
        shown = q(ex_gst * (1 + gst)) if incl else ex_gst
        truth = shown / (1 + gst) if incl else shown
        state = "inferred" if incl else "confirmed"
        note = "page 2 rates include GST @18%; ex-GST = shown / 1.18" if incl else ""
        cells[line["line_no"]] = cell(line, truth, Decimal("0"), state,
                                      f"Rs {shown}/pc" + (" incl. GST" if incl else ""), note)
        rows.append({"line_no": line["line_no"], "declined": False, "rate_shown": s(shown),
                     "gst_inclusive": incl,
                     "amount": s(q(shown * line["annual_qty"]))})
    true_sum = sum(dec(r["amount"]) for r in rows if not r["declined"])
    return {"payment_days": 90, "freight": "Included (FOR your plants)", "validity_days": 365,
            "cells": cells,
            "doc": {"rows": rows, "page_one_rows": page_one, "sum_of_lines": s(true_sum),
                    "stated_total": s(transpose_digits(rng, true_sum)),
                    "rebate_pct": "2", "rebate_threshold_cr": "3.5", "plate_charge": "4,500",
                    "quote_no": "SC/2425/311", "quote_date": "2025-02-22"}}


def transpose_digits(rng: random.Random, amount: Decimal) -> Decimal:
    """A typing slip: swap two adjacent, different digits of the rupee part."""
    digits = list(str(int(amount)))
    spots = [i for i in range(1, len(digits) - 3) if digits[i] != digits[i + 1]]
    i = rng.choice(spots)
    digits[i], digits[i + 1] = digits[i + 1], digits[i]
    return dec("".join(digits)) + (amount - int(amount))


def vendor_c(rng: random.Random, lines: list[dict], a: dict, b: dict) -> dict:
    """Word letter, USD per piece ex-works. Priced off the best of A and B so that it wins
    several lines ex-works and loses most of them once freight is added."""
    fx = dec(ASSUMPTIONS["fx.USDINR"]["value"])
    freight = Decimal("1.10")
    cells, usd_rows = {}, {}

    def ref(line):
        prices = [dec(v["cells"][line["line_no"]]["unit_price_inr"]) for v in (a, b)
                  if v["cells"][line["line_no"]]["quoted"]]
        return min(prices)

    big = [ln for ln in lines if ln["kind"] in ("5ply", "7ply")]
    near = [ln for ln in big if ln["plant"] != "Baddi"]
    rng.shuffle(near)
    plan = {}
    for i, line in enumerate(near):
        if i < 3:
            plan[line["line_no"]] = ("wins_landed", -u(rng, 1.6, 2.4))
        elif i < 3 + (len(near) - 3 + 1) // 2:
            plan[line["line_no"]] = ("wins_ex_works_only", -u(rng, 0.25, 0.85))
        else:
            plan[line["line_no"]] = ("loses", u(rng, 0.3, 1.0))
    for line in big:
        if line["plant"] == "Baddi":
            plan[line["line_no"]] = ("baddi_unknown_landed", -u(rng, 0.2, 0.8))

    small_3 = [ln for ln in lines if ln["kind"] == "3ply" and ln["spec"]["length_mm"] <= 300]
    # one uniform rate for every small 3-ply size: priced above the best local offer on each
    rule_usd = q(max([ref(ln) for ln in small_3], default=Decimal("10")) / fx * dec("1.03"),
                 "0.001")
    for line in lines:
        no = line["line_no"]
        fr = None if line["plant"] == "Baddi" else freight
        if no in plan:
            usd = q((ref(line) + plan[no][1]) / fx, "0.001")
            usd_rows[no] = {"usd": str(usd), "role": plan[no][0]}
            cells[no] = cell(line, usd * fx, fr, "inferred", f"USD {usd}/pc ex-works",
                             f"FX {fx} assumed; {plan[no][0].replace('_', ' ')}")
        elif line in small_3:
            cells[no] = cell(line, rule_usd * fx, fr, "inferred",
                             f"USD {rule_usd}/pc ex-works (uniform 3-ply rate)",
                             "rule: all 3-ply up to 300 mm length")
        else:
            cells[no] = cell(line, None, None, "not_quoted", None,
                             "3-ply above 300 mm 'on request'" if line["kind"] == "3ply"
                             else "not offered")
    return {"payment_days": 30, "freight": "Ex-works Mundra; approx INR 1.10/pc to Pune and "
            "Hosur, Baddi on actuals", "validity_days": 90, "cells": cells,
            "doc": {"usd_rows": usd_rows, "rule_usd_3ply": str(rule_usd), "freight_per_piece":
                    "1.10", "fx_clause_pct": "3", "quote_no": "PBI/EXP/0925",
                    "quote_date": "2025-02-20"}}


def vendor_d(rng: random.Random, lines: list[dict]) -> dict:
    """Phone photo of a rate card: per kg by ply, a few items per box, pads per 100."""
    rates = {3: dec(rng.choice(["36.50", "37.00", "37.50"])),
             5: dec(rng.choice(["39.50", "40.00", "40.50"])),
             7: dec(rng.choice(["41.00", "41.50"]))}
    smudged = f"{rates[7]:.2f}".replace("41", "44", 1)  # the "1" is smudged and may read as "4"
    five = [ln for ln in lines if ln["kind"] == "5ply"]
    light = rng.choice(five)
    printed_3 = [ln for ln in lines if ln["kind"] == "3ply" and ln["spec"]["print"] != "plain"]
    specials = rng.sample(printed_3 or [ln for ln in lines if ln["kind"] == "3ply"], 2)
    cells, special_rows = {}, []
    for line in lines:
        no, kg = line["line_no"], dec(line["ref_weight_g"]) / 1000
        fr = Decimal("0") if line["plant"] == "Pune" else None  # "free within 150 km"
        if line is light:
            price = q(dec(line["fair_price"]) * dec("0.80"))
            special_rows.append({"line_no": no, "price": str(price), "board": "120 GSM 16 BF",
                                 "deviation": True})
            cells[no] = cell(line, price, fr, "conflict", f"Rs {price} / box",
                             "board 120 GSM 16 BF quoted vs 150 GSM 20 BF specified")
        elif line in specials:
            price = q(dec(line["fair_price"]) * dec("0.93"))
            special_rows.append({"line_no": no, "price": str(price), "board": None,
                                 "deviation": False})
            cells[no] = cell(line, price, fr, "confirmed", f"Rs {price} / box")
        elif line["kind"] == "partition":
            per_box = int(q(dec(line["fair_price"]) * dec("0.92") * 100, "1"))
            cells[no] = cell(line, dec(per_box) / 100, fr, "needs_review",
                             f"Rs {per_box} / box", "'box' is a bundle of 100 sets")
            partition = per_box
        elif line["kind"] == "pad":
            per_100 = int(q(dec(line["fair_price"]) * dec("0.95") * 100, "1"))
            cells[no] = cell(line, dec(per_100) / 100, fr, "confirmed", f"Rs {per_100} / 100")
            pad = per_100
        else:
            ply = line["spec"]["ply"]
            state = "needs_review" if ply == 7 else "inferred"
            note = f"Rs {rates[ply]}/kg x {line['ref_weight_g']} g"
            if ply == 7:
                note += f"; smudged digit, could read {smudged}"
            cells[no] = cell(line, rates[ply] * kg, fr, state, f"Rs {rates[ply]} / kg", note)
    return {"payment_days": 30, "freight": "Free delivery within 150 km of Chakan",
            "validity_days": None, "cells": cells,
            "doc": {"rates_per_kg": {str(k): f"{v:.2f}" for k, v in rates.items()},
                    "smudged_7ply_reads": [f"{rates[7]:.2f}", smudged],
                    "special_items": special_rows, "light_line": light["line_no"],
                    "partition_per_box": partition, "box_means": 100, "pad_per_100": pad,
                    "card_date": "2025-02-01"}}


def vendor_e(rng: random.Random, lines: list[dict], history: list[dict]) -> dict:
    """One-line email: per-kg rules for 3- and 5-ply, 'rest same as last year', freight extra.

    Precedence: a rule for the line's ply wins; every other line takes E's own FY25 record.
    """
    rates = {3: dec(rng.choice(["38", "38.50"])), 5: dec(rng.choice(["41.50", "42", "42.50"]))}
    cells = {}
    for line in lines:
        no = line["line_no"]
        ply = line["spec"]["ply"] if line["kind"] in ("3ply", "5ply") else None
        own = own_record(history, "E", no)
        if ply:
            kg = dec(line["ref_weight_g"]) / 1000
            cells[no] = cell(line, rates[ply] * kg, None, "inferred", f"Rs {rates[ply]}/kg",
                             f"rule for all {ply}-ply lines x {line['ref_weight_g']} g")
        elif own:
            cells[no] = cell(line, dec(own["unit_price"]), None, "inferred", "same as last year",
                             f"from this vendor's own FY25 {own['source']} price "
                             f"Rs {own['unit_price']}")
        else:
            cells[no] = cell(line, None, None, "not_quoted", "same as last year",
                             "vendor has no FY25 record for this line; unresolved")
    return {"payment_days": None, "freight": "Extra (amount not stated)", "validity_days": None,
            "cells": cells, "doc": {"rates_per_kg": {str(k): str(v) for k, v in rates.items()},
                                    "email_date": "2025-02-25"}}


# --- Questionnaire --------------------------------------------------------------------

QUESTIONS = [
    (1, "Quality", "Do you hold a valid ISO 9001 certificate? Attach it.", "yes_no",
     {"op": "valid_on", "value": RFX["award_date"], "evidence": "certificate expiry date"}),
    (2, "Quality", "Do you test board strength in-house (burst and compression)?", "yes_no",
     {"op": "==", "value": "yes"}),
    (3, "Quality", "Rejection rate over the last 12 months?", "number", None),
    (4, "Quality", "Attach a recent board test report.", "file", None),
    (5, "Sustainability", "Do you hold FSC chain-of-custody certification? Attach it.",
     "yes_no", None),
    (6, "Sustainability", "What share of your paper is recycled?", "number", None),
    (7, "Capacity", "Monthly capacity in tonnes?", "number", None),
    (8, "Capacity", "Standard lead time in days?", "number", {"op": "<=", "value": "10"}),
    (9, "Capacity", "Will you hold two weeks of safety stock for us?", "yes_no", None),
    (10, "Commercial", "Do you accept 60-day payment terms? If not, state yours.", "text", None),
    (11, "Commercial", "How long are your prices valid?", "text", None),
    (12, "Commercial", "Is freight to our plants included? If not, state the charge.", "text",
     None),
]


def questionnaire() -> list[dict]:
    return [{"q_no": n, "section": sec, "text": text, "answer_type": at,
             "is_gating": rule is not None, "pass_rule": rule}
            for n, sec, text, at, rule in QUESTIONS]


# Each vendor's answers: q_no -> (answer text as they would write it, normalised value).
ANSWERS = {
    "A": {1: ("Yes – certificate attached", "yes"), 2: ("Yes – burst & BCT tester in-house",
          "yes"), 3: ("0.4%", "0.4"), 4: ("Attached (Jan-25)", "attached"),
          5: ("Yes – FSC CoC, certificate attached", "yes"), 6: ("65%", "65"),
          7: ("1,800 MT", "1800"), 8: ("7 days", "7"), 9: ("Yes", "yes"),
          10: ("Accepted", "60"), 11: ("Till 31.03.2026", "2026-03-31"),
          12: ("Included – rates FOR destination", "included")},
    "B": {1: ("Yes, copy enclosed", "yes"), 2: ("Yes. Burst factor and BCT tested every lot",
          "yes"), 3: ("Below 1%", "1"), 4: ("Will share on request", None),
          5: ("No", "no"), 6: ("Approx. 70%", "70"), 7: ("650 MT", "650"),
          8: ("10 days from PO", "10"), 9: ("Yes, 2 weeks", "yes"),
          10: ("Our standard terms are 90 days", "90"), 11: ("Up to 31.03.2026", "2026-03-31"),
          12: ("Included (FOR your plants)", "included")},
    "C": {1: ("We are ISO 9001:2015 certified; a copy of the certificate is enclosed.", "yes"),
          2: ("Our in-house laboratory tests bursting strength and box compression on every "
              "production lot.", "yes"),
          3: ("Customer rejections over the last twelve months were 0.6%.", "0.6"),
          5: ("Our Mundra unit holds FSC chain-of-custody certification (copy enclosed).",
              "yes"),
          6: ("Recycled fibre is about 55% of our paper intake.", "55"),
          7: ("Monthly converting capacity is 2,400 tonnes.", "2400"),
          8: ("Standard lead time is 8 days from receipt of PO, ex-works.", "8"),
          10: ("Payment: 30 days from invoice.", "30"),
          11: ("Prices are valid for 90 days.", "90 days"),
          12: ("Freight is not included; see the freight paragraph.", "not included")},
    "D": {1: ("ISO certified: yes (copy attached)", "yes"), 2: ("Testing in house: yes", "yes"),
          8: ("Lead time 5-7 days", "7"), 10: ("Payment 30 days", "30")},
    "E": {1: ("ISO cert attached.", "yes"), 7: ("Capacity no issue.", None)},
}

CERTIFICATES = {
    "A": [{"kind": "iso9001", "number": "QMS/IN/2024/11873", "issued": "2024-07-01",
           "expires": "2027-06-30"},
          {"kind": "fsc", "number": "FSC-C178214", "issued": "2021-12-01",
           "expires": "2026-11-30"},
          {"kind": "test_report", "number": "TR/KP/0125/07", "issued": "2025-01-18",
           "expires": None}],
    "B": [{"kind": "iso9001", "number": "QMS/IN/2023/09314", "issued": "2023-08-15",
           "expires": "2026-08-14"}],
    "C": [{"kind": "iso9001", "number": "QMS/IN/2024/00412", "issued": "2024-01-10",
           "expires": "2027-01-09"},
          {"kind": "fsc", "number": "FSC-C165503", "issued": "2021-06-01",
           "expires": "2026-05-31"}],
    "D": [{"kind": "iso9001", "number": "QMS/IN/2022/01977", "issued": "2022-02-01",
           "expires": "2025-01-31"},
          {"kind": "test_report", "number": "BC/LAB/24/112", "issued": "2024-12-02",
           "expires": None}],
    "E": [{"kind": "iso9001", "number": "QMS/IN/2023/12650", "issued": "2023-10-01",
           "expires": "2026-09-30"}],
}


def gating_verdict(q_no: int, answer: tuple | None, certs: list[dict]) -> str:
    """Pass rule -> pass / fail / unclear. A missing answer is unclear, never pass."""
    if answer is None or answer[1] is None:
        return "unclear"
    value = answer[1]
    if q_no == 1:
        iso = [c for c in certs if c["kind"] == "iso9001"]
        if value != "yes" or not iso:
            return "fail" if value == "no" else "unclear"
        return "pass" if iso[0]["expires"] >= RFX["award_date"] else "fail"
    if q_no == 2:
        return "pass" if value == "yes" else "fail"
    if q_no == 8:
        return "pass" if int(value) <= 10 else "fail"
    raise ValueError(q_no)


def overall_verdict(verdicts: dict[int, str]) -> str:
    gating = [verdicts[n] for n in GATING_QUESTIONS]
    if "fail" in gating:
        return "fail"
    return "unclear" if "unclear" in gating else "pass"


# --- Traps ----------------------------------------------------------------------------


def traps(lines: list[dict], v: dict) -> list[dict]:
    nos = lambda pred: [ln["line_no"] for ln in lines if pred(ln)]  # noqa: E731
    cells = lambda key, st: [c["line_no"] for c in v[key]["cells"].values()  # noqa: E731
                             if c["expected_state"] == st]
    b_rows = v["B"]["doc"]["rows"]
    d = v["D"]["doc"]
    e_ref = [c["line_no"] for c in v["E"]["cells"].values() if c["as_quoted"] ==
             "same as last year"]
    t = [
        ("X01", "A", nos(lambda ln: True), "Lines matched by spec, not position"),
        ("X02", "A", nos(lambda ln: True), "Per-1000 price converted; chain shown"),
        ("X03", "A", nos(lambda ln: True), "Slab matching annual quantity used; others kept"),
        ("X04", "A", nos(lambda ln: ln["plant"] == "Baddi"), "Both sheets read"),
        ("X05", "B", [], "Conditional rebate captured as a term; not applied by default"),
        ("X06", "B", cells("B", "not_quoted"), "Not quoted; never zero; gap stated"),
        ("X07", "B", [], "Stated grand total differs from sum of lines: arithmetic flag"),
        ("X08", "B", [r["line_no"] for r in b_rows if r.get("gst_inclusive")],
         "GST-inclusive rates detected, converted to ex-GST, marked inferred"),
        ("X09", "C", [c["line_no"] for c in v["C"]["cells"].values() if c["quoted"]],
         "USD converted at the pinned rate; FX shown as an assumption"),
        ("X10", "C", [], "Commercial terms extracted from paragraphs with quotes"),
        ("X11", "C", nos(lambda ln: ln["plant"] == "Baddi" and v["C"]["cells"][ln["line_no"]]
                         ["quoted"]), "Ex-works: landed from stated freight, else unknown"),
        ("X12", "C", [], "90-day validity and FX clause captured; in award caveats"),
        ("X13", "D", nos(lambda ln: True), "Photo read; double-read; low legibility to review"),
        ("X14", "D,E", [c["line_no"] for c in v["D"]["cells"].values()
                        if "/ kg" in (c["as_quoted"] or "")],
         "Per kg converted with ref_weight_g; marked inferred with the weight"),
        ("X15", "D", nos(lambda ln: ln["kind"] == "partition"),
         "'Per box' = bundle of 100: plausibility flag, clarification drafted"),
        ("X16", "D", [d["light_line"]], "Lighter board than specified: spec deviation conflict"),
        ("X17", "D", [], "ISO answered yes but certificate expired: conflict; gating fail"),
        ("X18", "D", nos(lambda ln: ln["kind"] == "7ply"), "Ambiguous digit: needs review"),
        ("X19", "E", e_ref, "Same as last year: every rest line from E's own FY25 record "
         "(contract first, else its FY25 quote), inferred; clarification asks to confirm"),
        ("X20", "E", [], "Freight extra, amount unstated: landed unknown, gap stated"),
        ("X21", "E", nos(lambda ln: ln["kind"] in ("3ply", "5ply")),
         "Rule quote applied to every line of that ply"),
        ("X22", "E", [], "Questionnaire mostly unanswered: verdict unclear"),
        ("X23", "A", [r["line_no"] for r in v["A"]["doc"]["revisions"]],
         "Revised quote supersedes; earlier kept in history"),
        ("X24", "all", [], "Payment terms differ: carrying cost in landed view"),
        ("X25", "B", [], "One-time plate charge captured as a term, not in unit price"),
        ("X26", "held-out", [], "Injection text ignored and flagged"),
    ]
    return [{"id": i, "vendor": vend, "lines": ls, "expected": exp} for i, vend, ls, exp in t]


# --- Assemble -------------------------------------------------------------------------


def build(seed: str) -> tuple[dict, list[dict]]:
    """Return (ground_truth, fy25_history) for one seed. History holds contracts and quotes."""
    rng = random.Random(f"level-dataset-{seed}")
    lines = build_lines(rng)
    contracts = assign_contracts(rng, lines)
    # Last year's losing bids. E bid every line; the others bid the lines they quote now.
    # A separate random stream, so adding quotes leaves this year's prices untouched.
    e_bids = losing_quotes(random.Random(f"level-history-{seed}"), lines, contracts,
                           {"E": {ln["line_no"] for ln in lines}})
    history = contracts + e_bids
    v = {"A": vendor_a(rng, lines), "B": vendor_b(rng, lines)}
    v["C"] = vendor_c(rng, lines, v["A"], v["B"])
    v["D"] = vendor_d(rng, lines)
    v["E"] = vendor_e(rng, lines, history)
    others = {k: {c["line_no"] for c in v[k]["cells"].values() if c["quoted"]} for k in "ABCD"}
    history += losing_quotes(random.Random(f"level-history-others-{seed}"), lines, contracts,
                             others)
    for key, vendor in v.items():
        vendor.update(VENDORS[key])
        vendor["key"] = key
        answers = ANSWERS[key]
        vendor["answers"] = {str(n): {"text": t, "value": val} for n, (t, val) in
                             answers.items()}
        vendor["certificates"] = CERTIFICATES[key]
        verdicts = {n: gating_verdict(n, answers.get(n), CERTIFICATES[key])
                    for n in GATING_QUESTIONS}
        vendor["gating"] = {"by_question": {str(n): vv for n, vv in verdicts.items()},
                            "overall": overall_verdict(verdicts)}
        vendor["cells"] = [vendor["cells"][ln["line_no"]] for ln in lines]
    truth = {
        "seed": seed,
        "buyer": BUYER,
        "rfx": RFX,
        "assumptions": ASSUMPTIONS,
        "lines": lines,
        "questionnaire": questionnaire(),
        "vendors": v,
        "history": history,
        "traps": [],
    }
    truth["traps"] = traps(lines, {k: {**vv, "cells": {c["line_no"]: c for c in vv["cells"]}}
                                   for k, vv in v.items()})
    return truth, history
