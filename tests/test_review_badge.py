"""The sidebar's Review count matches the Review queue (no model)."""

import pytest
import sqlalchemy as sa
from components import backend, data, mock_backend, mock_seed, review_data

from level import db


@pytest.fixture
def demo_event(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(backend, "rebuild_cells", mock_backend.rebuild_cells)
    return mock_seed.seed()


def test_review_badge_counts_what_the_queue_shows(demo_event):
    rid = demo_event
    with db.begin() as conn:
        vendor, line = conn.execute(sa.select(db.cell.c.vendor_id, db.cell.c.rfx_line_id)
                                    .where(db.cell.c.rfx_id == rid)).first()
        for kind, reason in [("plausibility", "price is 12x the median of other quotes"),
                             ("missing_info", "unit assumed: 1 box = 1 piece")]:
            conn.execute(db.flag.insert().values(
                rfx_id=rid, vendor_id=vendor, rfx_line_id=line, target_kind="cell",
                type=kind, severity="warning", reason=reason))
    left = sum(1 for i in review_data.issues(rid) if not i.decided)
    assert data.open_issue_count(rid) == left


def test_inbox_open_issues_count_what_the_queue_shows(demo_event):
    rid = demo_event
    rows = data.tracker(rid)
    assert sum(r["Open issues"] for r in rows) == data.open_issue_count(rid)
    left = [i for i in review_data.issues(rid) if not i.decided]
    for r in rows:
        assert r["Open issues"] == sum(1 for i in left if i.flag.vendor_id == r["vendor_id"])
