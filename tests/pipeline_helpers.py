"""A small non-packaging RFx for pipeline tests (safety supplies; units each and pair)."""

from pathlib import Path

from level import db

DOCS = Path(__file__).parent / "wp2_docs"

LINES = [
    (1, "Safety helmet, ABS, white", "each", "2000", {}, {"material": "ABS"}, "210"),
    (2, "Nitrile gloves, size M", "pair", "50000", {"pair_per_box": "50"}, {"size": "M"}, "4.2"),
    (3, "Hi-vis vest, size L", "each", "3000", {}, {"size": "L"}, "150"),
    (4, "Cable ties 300 x 4.8 mm", "each", "200000", {"each_per_pack": "100"},
     {"size_mm": "300x4.8"}, "0.9"),
]


def seed() -> dict:
    with db.begin() as conn:
        rfx = conn.execute(db.rfx.insert().values(
            title="Safety supplies FY27", category="safety", base_currency="INR",
            status="issued", terms={"payment_days": 60})).inserted_primary_key[0]
        ids = {}
        for no, desc, uom, qty, factors, spec, last in LINES:
            ids[no] = conn.execute(db.rfx_line.insert().values(
                rfx_id=rfx, line_no=no, description=desc, canonical_uom=uom, annual_qty=qty,
                conversion_factors=factors, spec=spec, last_price=last)).inserted_primary_key[0]
        vendors = {}
        for name in ("North", "South", "West", "East", "River"):
            vendors[name] = conn.execute(db.vendor.insert().values(name=name)
                                         ).inserted_primary_key[0]
            conn.execute(db.invitation.insert().values(rfx_id=rfx, vendor_id=vendors[name]))
        conn.execute(db.question.insert(), [
            {"rfx_id": rfx, "q_no": 1, "section": "Quality", "answer_type": "yes_no",
             "text": "Is your ISO 13485 registration current? Send a copy.", "is_gating": True,
             "pass_rule": {"op": "valid_on", "value": "2026-04-01"}},
            {"rfx_id": rfx, "q_no": 2, "section": "Capacity", "answer_type": "number",
             "text": "How many days from purchase order to dispatch?", "is_gating": True,
             "pass_rule": {"op": "<=", "value": "10"}},
        ])
        conn.execute(db.assumption.insert(), [
            {"key": "fx.USDINR", "value": "84", "source": "pinned"},
            {"key": "gst_rate", "value": "0.18", "source": "statute"},
            {"key": "cost_of_capital", "value": "0.12", "source": "finance"},
        ])
        # River supplied line 3 last period. North's record for line 1 must never be borrowed.
        conn.execute(db.price_history.insert().values(
            vendor_id=vendors["River"], rfx_line_id=ids[3], period="FY26", unit_price="149",
            currency="INR", uom="each", source="contract"))
        conn.execute(db.price_history.insert().values(
            vendor_id=vendors["North"], rfx_line_id=ids[1], period="FY26", unit_price="205",
            currency="INR", uom="each", source="contract"))
    return {"rfx": rfx, "lines": ids, "vendors": vendors}
