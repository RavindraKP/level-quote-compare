"""Background worker (PLAN §5 "Background worker design").

Pages call only two functions:
    enqueue(kind, target, file_hash=None) -> job_id   # returns at once
    get_job(job_id) -> Job | None                     # read progress

How it works:
- Jobs are rows in the `job` table. A page inserts a `queued` row and returns.
- One daemon thread per server process takes jobs one at a time. `start()` is guarded by a
  lock and by a check for a live thread with our name, so reruns, tabs and sessions never
  start a second one. (The app also wraps `start()` in Streamlit's resource cache.)
- A job is claimed with a single atomic UPDATE (queued -> running), so it cannot be taken twice,
  even by a second process.
- While a job runs, a heartbeat thread stamps `heartbeat_at` every few seconds. A job left
  `running` with a stale heartbeat (server killed mid-job) goes back to `queued` on start-up
  and on every idle loop, and is retried. After MAX_ATTEMPTS it is marked failed instead.
- Any exception is caught, stored on the row, and the loop moves on to the next job.
- A unique index on (kind:target:file_hash) for unfinished jobs makes a double-click or a
  refresh return the existing job. A finished or failed job does not block a new one, which
  is how "retry" works: enqueue the same thing again.

Handlers are plain functions registered with @handler("kind"). They must be idempotent:
a re-run overwrites its own rows. They report progress with ctx.progress(stage, fraction).
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from level import db
from level.types import Job, JobStatus

log = logging.getLogger("level.worker")

THREAD_NAME = "level-worker"
POLL_SECONDS = 0.5
HEARTBEAT_SECONDS = 3
# A running job with no heartbeat for this long is orphaned. Env override is for tests.
STALE_AFTER_SECONDS = float(os.environ.get("LEVEL_WORKER_STALE_SECONDS", "15"))
MAX_ATTEMPTS = 3
HANDLER_MODULES = ["level.jobs"]  # imported at start-up so their @handler calls register

_HANDLERS: dict[str, Callable[[JobContext], None]] = {}
_start_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API for pages
# ---------------------------------------------------------------------------


def enqueue(kind: str, target: str | int, file_hash: str | None = None) -> int:
    """Queue a job and return its id. Same kind+target+hash while unfinished -> same id."""
    target = str(target)
    key = f"{kind}:{target}:{file_hash or ''}"
    with db.begin() as conn:
        # INSERT OR IGNORE skips the insert if an unfinished job with this key exists.
        conn.execute(
            sa.text(
                "INSERT OR IGNORE INTO job (kind, target, dedupe_key, status, progress, attempts,"
                " created_at) VALUES (:kind, :target, :key, 'queued', 0, 0, :now)"
            ),
            {"kind": kind, "target": target, "key": key, "now": db.now_iso()},
        )
        job_id = conn.execute(
            sa.text(
                "SELECT id FROM job WHERE dedupe_key = :key AND status IN ('queued','running')"
                " ORDER BY id DESC LIMIT 1"
            ),
            {"key": key},
        ).scalar_one()
    return job_id


def get_job(job_id: int) -> Job | None:
    with db.connect() as conn:
        row = conn.execute(sa.select(db.job).where(db.job.c.id == job_id)).mappings().first()
    return Job.model_validate(dict(row)) if row else None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@dataclass
class JobContext:
    """What a handler receives: the job's target and a way to report progress."""

    job_id: int
    kind: str
    target: str
    attempt: int

    def progress(self, stage: str, fraction: float) -> None:
        """Record the current stage and progress (0..1). Also counts as a heartbeat."""
        with db.begin() as conn:
            conn.execute(
                db.job.update()
                .where(db.job.c.id == self.job_id)
                .values(stage=stage, progress=max(0.0, min(1.0, fraction)),
                        heartbeat_at=db.now_iso())
            )


def handler(kind: str):
    """Decorator: register a function as the handler for a job kind."""

    def register(fn: Callable[[JobContext], None]):
        _HANDLERS[kind] = fn
        return fn

    return register


def _load_handlers() -> None:
    for name in HANDLER_MODULES:
        importlib.import_module(name)


# ---------------------------------------------------------------------------
# Claiming, recovery, running
# ---------------------------------------------------------------------------


def claim_next() -> JobContext | None:
    """Atomically move the oldest queued job to running and return it, or None."""
    now = db.now_iso()
    with db.begin() as conn:
        # One statement, so two threads or processes can never claim the same row.
        row = conn.execute(
            sa.text(
                "UPDATE job SET status='running', attempts=attempts+1, started_at=:now,"
                " heartbeat_at=:now, error=NULL"
                " WHERE id = (SELECT id FROM job WHERE status='queued' ORDER BY id LIMIT 1)"
                " AND status='queued'"
                " RETURNING id, kind, target, attempts"
            ),
            {"now": now},
        ).first()
    if row is None:
        return None
    return JobContext(job_id=row.id, kind=row.kind, target=row.target, attempt=row.attempts)


def recover_stale(stale_after: float | None = None) -> int:
    """Re-queue running jobs whose heartbeat stopped (e.g. the server was killed).

    Jobs that have already used MAX_ATTEMPTS are marked failed so a job that crashes the
    process cannot loop forever. Returns how many jobs were re-queued.
    """
    stale_after = STALE_AFTER_SECONDS if stale_after is None else stale_after
    cutoff = (datetime.now(UTC) - timedelta(seconds=stale_after)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    stale = sa.and_(
        db.job.c.status == "running",
        sa.or_(db.job.c.heartbeat_at.is_(None), db.job.c.heartbeat_at < cutoff),
    )
    with db.begin() as conn:
        conn.execute(
            db.job.update()
            .where(stale, db.job.c.attempts >= MAX_ATTEMPTS)
            .values(status="failed", finished_at=db.now_iso(),
                    error=f"Stopped after {MAX_ATTEMPTS} attempts (server restarted mid-job).")
        )
        result = conn.execute(
            db.job.update().where(stale).values(status="queued", stage="re-queued after restart")
        )
    if result.rowcount:
        log.warning("re-queued %d stale job(s)", result.rowcount)
    return result.rowcount


def _finish(job_id: int, status: JobStatus, error: str | None = None) -> None:
    values = {"status": status.value, "finished_at": db.now_iso(), "error": error}
    if status == JobStatus.DONE:
        values["progress"] = 1.0
    with db.begin() as conn:
        conn.execute(db.job.update().where(db.job.c.id == job_id).values(**values))


def _heartbeat_loop(job_id: int, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            with db.begin() as conn:
                conn.execute(
                    db.job.update()
                    .where(db.job.c.id == job_id, db.job.c.status == "running")
                    .values(heartbeat_at=db.now_iso())
                )
        except Exception:  # a missed beat is harmless; the next one will land
            log.exception("heartbeat failed for job %s", job_id)


def run_job(ctx: JobContext) -> None:
    """Run one claimed job and record done or failed. Never raises."""
    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat_loop, args=(ctx.job_id, stop), daemon=True)
    beat.start()
    try:
        fn = _HANDLERS.get(ctx.kind)
        if fn is None:
            raise LookupError(f"No handler registered for job kind '{ctx.kind}'")
        fn(ctx)
        _finish(ctx.job_id, JobStatus.DONE)
    except Exception as exc:
        # Keep the message plus the last few frames: enough to debug, short enough to show.
        tb = "".join(traceback.format_exception(exc, limit=-4))
        log.error("job %s (%s) failed: %s", ctx.job_id, ctx.kind, exc)
        _finish(ctx.job_id, JobStatus.FAILED, f"{type(exc).__name__}: {exc}\n\n{tb}")
    finally:
        stop.set()
        beat.join(timeout=HEARTBEAT_SECONDS + 1)


def run_once() -> bool:
    """Claim and run one job. Returns False if the queue was empty."""
    ctx = claim_next()
    if ctx is None:
        return False
    run_job(ctx)
    return True


def _loop() -> None:
    log.info("worker started")
    while True:
        try:
            if not run_once():
                recover_stale()
                time.sleep(POLL_SECONDS)
        except Exception:  # e.g. database briefly unavailable; keep the thread alive
            log.exception("worker loop error")
            time.sleep(2)


def _running_thread() -> threading.Thread | None:
    return next((t for t in threading.enumerate() if t.name == THREAD_NAME and t.is_alive()), None)


def start() -> threading.Thread:
    """Start the worker thread once per process and return it. Safe to call on every rerun."""
    with _start_lock:
        existing = _running_thread()
        if existing is not None:
            return existing
        db.init()
        _load_handlers()
        recover_stale()
        t = threading.Thread(target=_loop, name=THREAD_NAME, daemon=True)
        t.start()
        return t
