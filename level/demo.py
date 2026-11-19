"""Demo state: save the processed event once, put it back in seconds later.

    uv run python -m level.demo status
    uv run python -m level.demo reseed     # empty the database, load seed A, queue every reply
    uv run python -m level.demo read       # reseed, then read every reply here (calls the model)
    uv run python -m level.demo save       # keep the current database as the demo state
    uv run python -m level.demo restore    # put the saved demo state back

The saved state is a copy of the SQLite file plus the original documents it points to, kept
next to the database (on the host's persistent disk). Restoring makes no model calls; the
numbers in it were produced by the real pipeline when the state was saved.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import sqlalchemy as sa

from level import db


class Busy(RuntimeError):
    """Raised when a job is queued or running: changing the database now would lose it."""


def snapshot_dir() -> Path:
    """Folder holding the saved demo state, next to the database."""
    return db.db_path().parent / "demo-snapshot"


def info() -> dict | None:
    """What the saved state holds ({saved_at, messages, cells}), or None if nothing is saved."""
    meta = snapshot_dir() / "meta.json"
    return json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else None


def active_jobs() -> int:
    """Jobs queued or running right now."""
    with db.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(db.job).where(
            db.job.c.status.in_(["queued", "running"]))).scalar()


def _refuse_if_busy() -> None:
    if active_jobs():
        raise Busy("A job is still queued or running. Wait for it to finish, then try again.")


def _copy_db(src: Path, dst: Path) -> None:
    """Consistent copy of a live SQLite file (WAL included) using SQLite's own backup."""
    source, target = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()


def save() -> dict:
    """Keep the current database and its documents as the demo state."""
    _refuse_if_busy()
    with db.connect() as conn:  # a visitor's sandbox must never become part of the demo
        if conn.execute(sa.select(sa.func.count()).select_from(db.rfx).where(
                db.rfx.c.is_sandbox)).scalar():
            raise Busy("A sandbox is open. Delete every sandbox first, then save the copy.")
    target = snapshot_dir()
    if target.exists():
        shutil.rmtree(target)
    (target / "uploads").mkdir(parents=True)
    _copy_db(db.db_path(), target / "level.db")
    with db.connect() as conn:
        hashes = conn.execute(sa.select(db.attachment.c.sha256).distinct()).scalars().all()
        meta = {
            "saved_at": db.now_iso(),
            "messages": conn.execute(sa.select(sa.func.count()).select_from(db.message)).scalar(),
            "cells": conn.execute(sa.select(sa.func.count()).select_from(db.cell)).scalar(),
        }
    for sha in hashes:
        src = db.upload_dir() / sha
        if src.exists():
            shutil.copyfile(src, target / "uploads" / sha)
    (target / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def restore() -> dict:
    """Replace the database with the saved demo state. Raises if nothing is saved."""
    meta = info()
    if meta is None:
        raise FileNotFoundError("No demo state saved yet. Reseed, read the inbox, then save.")
    _refuse_if_busy()
    for f in (snapshot_dir() / "uploads").iterdir():
        dst = db.upload_dir() / f.name
        if not dst.exists():  # files are named by their hash, so an existing one is identical
            shutil.copyfile(f, dst)
    _copy_db(snapshot_dir() / "level.db", db.db_path())
    db.init()  # tables added since the state was saved
    with db.begin() as conn:
        # Point every attachment at this machine's upload folder (the saved paths may differ).
        for att_id, sha in conn.execute(sa.select(db.attachment.c.id, db.attachment.c.sha256)):
            conn.execute(db.attachment.update().where(db.attachment.c.id == att_id)
                         .values(path=str(db.upload_dir() / sha)))
    return meta


def reseed() -> list[int]:
    """Empty the database, load seed A unread, queue every reply. Returns the job ids."""
    _refuse_if_busy()
    from level.pipeline import run  # here, so `status` and `restore` need no pipeline imports

    if str(db.ROOT) not in sys.path:  # scripts/ is not an installed package
        sys.path.insert(0, str(db.ROOT))
    from scripts import seed  # the dataset loader (WP1); level/ holds no fixture knowledge

    db.reset()
    seed.load("A")
    with db.connect() as conn:
        ids = conn.execute(sa.select(db.message.c.id).where(db.message.c.direction == "in")
                           .order_by(db.message.c.received_at)).scalars().all()
    return [run.enqueue(i) for i in ids]


def _main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        print(f"saved demo state: {info() or 'none'} · active jobs: {active_jobs()}")
    elif cmd == "save":
        print(f"saved: {save()}")
    elif cmd == "restore":
        print(f"restored state saved at {restore()['saved_at']}")
    elif cmd in ("reseed", "read"):
        jobs = reseed()
        print(f"reseeded; {len(jobs)} replies queued")
        if cmd == "read":
            from level import jobs as _handlers  # noqa: F401  registers the pipeline handler
            from level import worker

            while worker.run_once():  # the worker's own job code, one reply at a time
                print("  reply read")
    else:
        print("usage: python -m level.demo [status|reseed|read|save|restore]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
