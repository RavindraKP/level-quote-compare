"""Demo state save / restore / reseed (WP8). No model calls."""

import pytest
import sqlalchemy as sa

from level import db, demo, worker
from level.pipeline import run


@pytest.fixture
def disk(tmp_db, tmp_path, monkeypatch):
    """Database and uploads both on a temporary 'persistent disk'."""
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    return tmp_path


def _one_reply() -> int:
    with db.begin() as conn:
        rfx_id = conn.execute(db.rfx.insert().values(
            title="Test event", category="test", base_currency="INR", status="collecting",
        )).inserted_primary_key[0]
        vendor_id = conn.execute(db.vendor.insert().values(name="V1")).inserted_primary_key[0]
    return run.ingest(rfx_id, vendor_id, files=[("q.txt", b"price 10")], body="hello")


def _count(table) -> int:
    with db.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar()


def test_restore_brings_back_the_saved_state(disk, tmp_path, monkeypatch):
    _one_reply()
    meta = demo.save()
    assert meta["messages"] == 1 and demo.info() == meta

    db.reset()  # someone wipes the event during a rehearsal
    assert _count(db.message) == 0

    # The state is restored onto a machine whose upload folder is elsewhere.
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "other-uploads"))
    demo.restore()
    assert _count(db.message) == 1
    with db.connect() as conn:
        path = conn.execute(sa.select(db.attachment.c.path)).scalar()
    assert path.startswith(str(tmp_path / "other-uploads"))
    assert open(path, "rb").read() == b"price 10"


def test_restore_without_saved_state_says_so(disk):
    with pytest.raises(FileNotFoundError):
        demo.restore()


def test_busy_worker_blocks_save_and_restore(disk):
    _one_reply()
    demo.save()
    worker.enqueue("demo.sleep", "1", file_hash="busy")
    with pytest.raises(demo.Busy):
        demo.save()
    with pytest.raises(demo.Busy):
        demo.restore()


def test_reseed_loads_the_event_and_queues_every_reply(disk):
    jobs = demo.reseed()
    assert jobs and len(jobs) == _count(db.message)
    assert demo.active_jobs() == len(jobs)
