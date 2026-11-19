"""Screens -> real backend: a Review decision reaches the real cell rebuild (WP4 x WP3)."""

import json

import sqlalchemy as sa
from components import backend, data

from level import db
from level.core.cells import rebuild_cells
from tests import pipeline_helpers as H


def _event():
    ids = H.seed()
    v, line = ids["vendors"]["North"], ids["lines"][1]
    with db.begin() as conn:
        conn.execute(db.invitation.update().where(db.invitation.c.vendor_id == v)
                     .values(status="replied"))
        cid = conn.execute(db.quote_claim.insert().values(
            rfx_id=ids["rfx"], vendor_id=v, vendor_item_text="helmet", raw_price="21O",
            raw_currency="INR", basis={"per": "each", "multiple": None}, scope="line",
            evidence_locator="email, line 1", evidence_quote="helmet 21O",
            evidence_ok=False)).inserted_primary_key[0]
        conn.execute(db.claim_line.insert().values(claim_id=cid, rfx_line_id=line))
        fid = conn.execute(db.flag.insert().values(
            rfx_id=ids["rfx"], vendor_id=v, target_kind="claim", target_id=cid,
            rfx_line_id=line, type="evidence", severity="warning",
            reason="price digits not in the quote")).inserted_primary_key[0]
    rebuild_cells(ids["rfx"])
    return ids, v, line, fid


def _cell(v, line):
    with db.connect() as conn:
        query = sa.select(db.cell).where(db.cell.c.vendor_id == v, db.cell.c.rfx_line_id == line)
        return conn.execute(query).mappings().one()


def test_backend_is_the_real_one():
    assert backend.PIPELINE_JOB_KIND == "pipeline.process_message"
    assert backend.job_target(7) == "7"  # what level.pipeline.run's job handler parses


def test_buyer_correction_from_review_rebuilds_real_cell(tmp_db):
    ids, v, line, fid = _event()
    assert _cell(v, line)["state"] == "needs_review"
    data.resolve_flag(fid, "edit_value", "210", note="typo O for 0")
    cell = _cell(v, line)
    assert cell["unit_price_inr"] == "210.0000" and cell["state"] == "confirmed"
    with db.connect() as conn:
        res = conn.execute(sa.select(db.flag.c.resolution).where(db.flag.c.id == fid)).scalar()
    assert json.loads(res)["action"] == "edit_value"


def test_mark_not_quoted_from_review(tmp_db):
    ids, v, line, fid = _event()
    data.resolve_flag(fid, "not_quoted")
    cell = _cell(v, line)
    assert cell["state"] == "not_quoted" and cell["unit_price_inr"] is None


def test_review_correction_is_per_line_unit_even_for_a_per_kg_claim(tmp_db):
    """The Review screen asks for INR per RFx unit; a per-kg claim must not re-convert it."""
    ids, v, line, fid = _event()
    with db.begin() as conn:  # make the claim per kg on a line with a weight factor
        conn.execute(db.rfx_line.update().where(db.rfx_line.c.id == line).values(
            conversion_factors={"g_per_each": "500"}))
        conn.execute(db.quote_claim.update().values(basis={"per": "kg", "multiple": None}))
    data.resolve_flag(fid, "edit_value", "210")
    assert _cell(v, line)["unit_price_inr"] == "210.0000"
