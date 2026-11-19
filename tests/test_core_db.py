"""rebuild_cells, the analyst views and the exports against a real SQLite file."""

from io import BytesIO

import sqlalchemy as sa
from openpyxl import load_workbook

from level import db
from level.core import cells as cells_mod
from level.core.export import comparison_xlsx, scenario_xlsx
from level.core.scenario import run_scenario
from level.types import ScenarioParams


def seed_small(conn) -> int:
    rfx = db.rfx.insert().values(title="Test", base_currency="INR", terms={"payment_days": 60})
    rfx_id = conn.execute(rfx).inserted_primary_key[0]
    v1 = conn.execute(db.vendor.insert().values(name="North")).inserted_primary_key[0]
    v2 = conn.execute(db.vendor.insert().values(name="South")).inserted_primary_key[0]
    conn.execute(db.invitation.insert(), [{"rfx_id": rfx_id, "vendor_id": v1, "status": "replied"},
                                          {"rfx_id": rfx_id, "vendor_id": v2, "status": "invited"}])
    line = conn.execute(db.rfx_line.insert().values(
        rfx_id=rfx_id, line_no=1, description="Widget", canonical_uom="each", annual_qty="1200",
        conversion_factors={}, last_price="11")).inserted_primary_key[0]
    claim = conn.execute(db.quote_claim.insert().values(
        rfx_id=rfx_id, vendor_id=v1, vendor_item_text="Widget", raw_price="120",
        raw_currency="INR", basis={"per": "dozen", "multiple": None}, scope="line",
        evidence_locator="email, line 1", evidence_quote="Widget Rs 120 per dozen",
        evidence_ok=True)).inserted_primary_key[0]
    conn.execute(db.claim_line.insert().values(claim_id=claim, rfx_line_id=line))
    conn.execute(db.vendor_term.insert().values(rfx_id=rfx_id, vendor_id=v1, type="freight",
                                                text="Freight included"))
    return rfx_id


def test_rebuild_views_exports(tmp_db):
    with db.begin() as conn:
        rfx_id = seed_small(conn)
    cells = cells_mod.rebuild_cells(rfx_id)
    cells_mod.rebuild_cells(rfx_id)  # idempotent: rebuilt, never duplicated
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM cell")).scalar() == 2
    with db.connect_readonly() as ro:
        rows = ro.execute(sa.text("SELECT vendor, state, unit_price_inr FROM v_cells "
                                  "ORDER BY vendor")).all()
        assert [tuple(r) for r in rows] == [("North", "confirmed", "10.0000"),
                                            ("South", "awaiting", None)]
        for view in ("v_lines", "v_vendors", "v_terms", "v_questionnaire", "v_flags",
                     "v_history"):
            ro.execute(sa.text(f"SELECT * FROM {view}")).all()
    assert len(cells) == 2

    snap = cells_mod.load_snapshot(rfx_id)
    wb = load_workbook(BytesIO(comparison_xlsx(snap)))
    assert wb.sheetnames[:5] == ["As quoted", "Per unit (INR)", "Delivered (INR)", "Issues",
                                 "Assumptions"]
    assert wb["Per unit (INR)"]["E2"].value == 10.0
    assert wb["Per unit (INR)"]["F2"].value == "Awaiting reply"  # no number without a reply

    result = run_scenario(rfx_id, ScenarioParams(strategy="cheapest_per_line"))
    assert result.total_inr == 12000
    assert load_workbook(BytesIO(scenario_xlsx(result, snap)))["Allocation"]["C2"].value == "North"


def test_history_view_shows_source_and_contract_comes_first(tmp_db):
    with db.begin() as conn:
        rfx_id = seed_small(conn)
        line = conn.execute(sa.select(db.rfx_line.c.id)).scalar()
        south = conn.execute(sa.select(db.vendor.c.id).where(db.vendor.c.name == "South")
                             ).scalar()
        conn.execute(db.price_history.insert(), [
            {"vendor_id": south, "rfx_line_id": line, "period": "FY26", "unit_price": "11",
             "currency": "INR", "uom": "each", "source": "contract"},
            {"vendor_id": south, "rfx_line_id": line, "period": "FY26", "unit_price": "13",
             "currency": "INR", "uom": "each", "source": "quote"}])
    cells_mod.rebuild_cells(rfx_id)  # installs the views
    with db.connect_readonly() as ro:
        rows = ro.execute(sa.text("SELECT vendor, source, unit_price FROM v_history "
                                  "WHERE rfx_id = :r ORDER BY source"), {"r": rfx_id}).all()
    assert [tuple(r) for r in rows] == [("South", "contract", "11"), ("South", "quote", "13")]
    snap = cells_mod.load_snapshot(rfx_id)
    assert snap.history[(south, line)]["unit_price"] == "11"  # contract before a later quote
