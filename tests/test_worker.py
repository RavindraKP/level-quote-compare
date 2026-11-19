"""Worker checks from WP0's acceptance list (the UI checks are done by hand / Playwright)."""

import os
import signal
import subprocess
import sys
import threading
import time

import sqlalchemy as sa

import level.jobs  # noqa: F401  registers demo handlers
from level import db, worker
from level.types import JobStatus


def _runs(job_id: int) -> int:
    """How many times the demo handler actually started for this job."""
    with db.connect() as conn:
        return conn.execute(
            sa.select(sa.func.count()).select_from(db.audit_event).where(
                db.audit_event.c.action == "demo.sleep.run", db.audit_event.c.target_id == job_id
            )
        ).scalar()


def test_enqueue_same_request_returns_same_job(tmp_db):
    a = worker.enqueue("demo.sleep", "1", file_hash="h")
    b = worker.enqueue("demo.sleep", "1", file_hash="h")
    c = worker.enqueue("demo.sleep", "1", file_hash="other")
    assert a == b != c


def test_concurrent_enqueue_creates_one_job(tmp_db):
    ids: list[int] = []
    threads = [threading.Thread(target=lambda: ids.append(worker.enqueue("demo.sleep", "1")))
               for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 1


def test_each_job_is_claimed_exactly_once(tmp_db):
    for i in range(30):
        worker.enqueue("demo.sleep", str(i))
    claimed: list[int] = []
    lock = threading.Lock()

    def grab():
        while (ctx := worker.claim_next()) is not None:
            with lock:
                claimed.append(ctx.job_id)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 30 and len(set(claimed)) == 30


def test_finished_job_allows_a_retry(tmp_db):
    first = worker.enqueue("demo.fail", "x")
    worker.run_once()
    second = worker.enqueue("demo.fail", "x")
    assert second != first


def test_failed_job_records_error_and_queue_continues(tmp_db):
    bad = worker.enqueue("demo.fail", "boom")
    good = worker.enqueue("demo.sleep", "1")
    assert worker.run_once() and worker.run_once()
    bad_job, good_job = worker.get_job(bad), worker.get_job(good)
    assert bad_job.status == JobStatus.FAILED
    assert "deliberate failure for target boom" in bad_job.error
    assert good_job.status == JobStatus.DONE and good_job.progress == 1.0
    assert _runs(good) == 1


def test_unknown_kind_fails_cleanly(tmp_db):
    job_id = worker.enqueue("no.such.kind", "1")
    worker.run_once()
    assert "No handler" in worker.get_job(job_id).error


def test_stale_running_job_is_requeued(tmp_db):
    job_id = worker.enqueue("demo.sleep", "1")
    worker.claim_next()
    with db.begin() as conn:  # simulate a crash: running, heartbeat long ago
        conn.execute(db.job.update().where(db.job.c.id == job_id)
                     .values(heartbeat_at="2000-01-01T00:00:00.000000Z"))
    assert worker.recover_stale() == 1
    assert worker.get_job(job_id).status == JobStatus.QUEUED


def test_fresh_running_job_is_left_alone(tmp_db):
    job_id = worker.enqueue("demo.sleep", "1")
    worker.claim_next()
    assert worker.recover_stale() == 0
    assert worker.get_job(job_id).status == JobStatus.RUNNING


def test_job_that_keeps_crashing_is_eventually_failed(tmp_db):
    job_id = worker.enqueue("demo.sleep", "1")
    with db.begin() as conn:
        conn.execute(db.job.update().where(db.job.c.id == job_id).values(
            status="running", attempts=worker.MAX_ATTEMPTS,
            heartbeat_at="2000-01-01T00:00:00.000000Z"))
    worker.recover_stale()
    job = worker.get_job(job_id)
    assert job.status == JobStatus.FAILED and "attempts" in job.error


def test_start_is_idempotent(tmp_db):
    threads = {worker.start() for _ in range(5)}
    assert len(threads) == 1
    names = [t.name for t in threading.enumerate() if t.name == worker.THREAD_NAME]
    assert len(names) == 1


SERVER = """
import time
from level import worker
worker.start()
while True:
    time.sleep(1)
"""


def _spawn(env):
    return subprocess.Popen([sys.executable, "-c", SERVER], env=env)


def _wait(job_id, cond, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = worker.get_job(job_id)
        if cond(job):
            return job
        time.sleep(0.2)
    raise AssertionError(f"timed out; job is {worker.get_job(job_id)}")


def test_job_finishes_after_server_is_killed_mid_job(tmp_db):
    env = {**os.environ, "LEVEL_WORKER_STALE_SECONDS": "4"}
    job_id = worker.enqueue("demo.sleep", "4")

    first = _spawn(env)
    _wait(job_id, lambda j: j.status == JobStatus.RUNNING and j.progress > 0, 20)
    first.send_signal(signal.SIGKILL)  # hard kill: no cleanup, row stays "running"
    first.wait()
    assert worker.get_job(job_id).status == JobStatus.RUNNING

    second = _spawn(env)
    try:
        job = _wait(job_id, lambda j: j.status in (JobStatus.DONE, JobStatus.FAILED), 40)
    finally:
        second.kill()
        second.wait()
    assert job.status == JobStatus.DONE
    assert job.attempts == 2
    assert _runs(job_id) == 2  # one interrupted run, one complete run
