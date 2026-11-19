"""Real model calls on two small hand-made replies (~$0.001 on Haiku). Skipped without a key."""

import os

import pytest
import sqlalchemy as sa

from level import db
from level.pipeline import run
from tests.pipeline_helpers import DOCS, seed

pytestmark = [pytest.mark.live, pytest.mark.skipif(
    not os.environ.get("LEVEL_ANTHROPIC_API_KEY"), reason="LEVEL_ANTHROPIC_API_KEY not set")]


@pytest.fixture
def env(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    return seed()


def cells(vendor_id):
    with db.connect() as conn:
        return {r.line_no: r for r in conn.execute(sa.text(
            "SELECT l.line_no, c.state, c.unit_price_inr, c.landed_price_inr FROM cell c "
            "JOIN rfx_line l ON l.id = c.rfx_line_id WHERE c.vendor_id = :v"), {"v": vendor_id})}


def test_live_email_rule_reference_and_freight(env):
    v = env["vendors"]["River"]
    mid = run.ingest(env["rfx"], v, body=(DOCS / "email_body.txt").read_text(encoding="utf-8"))
    summary = run.process_message(mid)
    assert summary["claims"] >= 2 and not summary["unreadable"]
    c = cells(v)
    assert c[2].unit_price_inr == "4.0500"  # stated per pair
    assert c[3].state == "inferred" and c[3].unit_price_inr == "149.0000"  # own history
    assert c[1].state == "not_quoted" and c[1].unit_price_inr is None  # no history: unresolved
    assert c[2].landed_price_inr is None  # carriage charged separately, amount unstated


def test_live_sideways_photo(env):
    v = env["vendors"]["East"]
    data = (DOCS / "east_photo_sideways.jpg").read_bytes()
    run.process_message(run.ingest(env["rfx"], v, files=[("photo.jpg", data)]))
    c = cells(v)
    assert c[1].unit_price_inr == "219.0000" and c[3].unit_price_inr == "148.0000"
    assert c[2].unit_price_inr == "3.9200"  # Rs 196 per box of 50 pairs
    with db.connect() as conn:
        stages = set(conn.execute(sa.select(db.extraction_run.c.stage)).scalars())
    assert {"extract_image", "extract_image_2"} <= stages  # read twice
