"""Read every inbound reply that has not been read yet, through the real worker job.

    uv run python scripts/read_inbox.py          # after scripts/seed.py

Same path as the Inbox page: each reply is queued with level.pipeline.run.enqueue and run
by the worker's own job code (worker.run_once), so job rows, progress and timings are real.
Used to prepare the demo event ("five replies already read"); the app can then start with
results already in SQLite. Nothing here is cached or faked: every reply calls the model.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from level import db, jobs, worker  # noqa: E402,F401  (jobs registers the pipeline handler)
from level.pipeline import run  # noqa: E402


def unread_messages() -> list[int]:
    """Inbound messages with no finished pipeline job."""
    with db.connect() as conn:
        ids = conn.execute(sa.select(db.message.c.id).where(db.message.c.direction == "in")
                           .order_by(db.message.c.received_at)).scalars().all()
        done = set(conn.execute(sa.select(db.job.c.target).where(
            db.job.c.kind == run.JOB_KIND, db.job.c.status == "done")).scalars())
    return [i for i in ids if str(i) not in done]


def main() -> int:
    pending = unread_messages()
    print(f"{len(pending)} unread repl{'y' if len(pending) == 1 else 'ies'}")
    for message_id in pending:
        run.enqueue(message_id)
    started = time.monotonic()
    while worker.run_once():  # claims and runs one queued job; False when the queue is empty
        print(f"  job finished after {time.monotonic() - started:.0f}s")
    with db.connect() as conn:
        failed = conn.execute(sa.select(db.job.c.target, db.job.c.error).where(
            db.job.c.kind == run.JOB_KIND, db.job.c.status == "failed")).all()
    for target, error in failed:
        print(f"  message {target} FAILED: {error}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
