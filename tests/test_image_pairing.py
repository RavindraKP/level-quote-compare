"""Pairing two photo reads: the same item worded a little differently is the same row, and
a row the second read did not find is settled by the zoom re-read only when it agrees with
the first read. No API key needed (fixed outputs, see tests/test_pipeline.py)."""

import pytest
import sqlalchemy as sa

from level import db
from level.pipeline import run, vision
from tests.pipeline_helpers import DOCS, seed
from tests.test_pipeline import install_fakes, output, row


@pytest.fixture
def env(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    return seed()


def test_same_item_worded_differently_is_paired_and_prices_compared():
    first = output([row("Safety helmet ABS, white", "219", "q")])
    same = output([row("Safety helmet ABS white.", "219", "q")])
    other = output([row("Safety helmet - ABS white", "279", "q")])
    assert vision.compare_reads(first, same) == []
    diff = vision.compare_reads(first, other)
    assert [(d.first, d.second) for d in diff] == [("219", "279")]  # still a real difference


def test_another_size_of_the_same_item_is_not_paired():
    first = output([row("Carton 5 ply 300x200x150", "21", "q")])
    second = output([row("Carton 5-ply 300x200x100", "21", "q")])
    assert [(d.first, d.second) for d in vision.compare_reads(first, second)] == [("21", None)]


def test_unrelated_item_is_not_paired():
    first = output([row("Safety helmet ABS white", "219", "q")])
    second = output([row("Cable ties 300 x 4.8 mm", "219", "q")])
    assert [(d.first, d.second) for d in vision.compare_reads(first, second)] == [("219", None)]


def _photo(env, monkeypatch, first, second, zoom):
    install_fakes(monkeypatch, [first, second], zoom_price=zoom)
    data = (DOCS / "east_photo_sideways.jpg").read_bytes()
    mid = run.ingest(env["rfx"], env["vendors"]["East"], files=[("photo.jpg", data)])
    run.process_message(mid)
    with db.connect() as conn:
        flags = conn.execute(sa.select(db.flag).where(db.flag.c.type == "read_mismatch")
                             ).mappings().all()
        cell = conn.execute(sa.select(db.cell).where(
            db.cell.c.vendor_id == env["vendors"]["East"],
            db.cell.c.rfx_line_id == env["lines"][1])).mappings().one()
    return flags, cell


def test_row_missing_from_second_read_settled_when_zoom_agrees(env, monkeypatch):
    first = output([row("Safety helmet ABS white", "219", "Rs 219 / each")])
    flags, cell = _photo(env, monkeypatch, first, output([]), zoom="219")
    assert flags == []
    assert cell["state"] != "needs_review"


def test_row_missing_from_second_read_stays_in_review_when_zoom_differs(env, monkeypatch):
    first = output([row("Safety helmet ABS white", "219", "Rs 219 / each")])
    flags, cell = _photo(env, monkeypatch, first, output([]), zoom="279")
    assert len(flags) == 1 and set(flags[0]["candidates"]) == {"219", "279"}
    assert cell["state"] == "needs_review"  # never silently chosen
