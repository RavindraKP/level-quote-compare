"""Job handlers, registered with the worker.

Each package adds its handler here (e.g. WP2: "pipeline.process_attachment"), so the worker
can import one module at start-up and find them all. Handlers must be idempotent.
"""

from __future__ import annotations

import time

from level import db
from level.agents.analyst import agent as _analyst  # noqa: F401 - registers analyst.ask
from level.pipeline import run as _pipeline_run  # noqa: F401 - registers WP2's job
from level.worker import JobContext, handler


@handler("demo.sleep")
def demo_sleep(ctx: JobContext) -> None:
    """Self-test job: sleeps for `target` seconds (default 20), reporting progress.

    Writes one audit_event per run so tests can count how many times it actually ran.
    """
    seconds = int(ctx.target) if ctx.target.isdigit() else 20
    with db.begin() as conn:
        conn.execute(
            db.audit_event.insert().values(
                actor="system", action="demo.sleep.run", target_kind="job", target_id=ctx.job_id,
                after={"attempt": ctx.attempt},
            )
        )
    for i in range(seconds):
        ctx.progress(f"sleeping {i + 1}/{seconds}s", (i + 1) / seconds)
        time.sleep(1)


@handler("demo.fail")
def demo_fail(ctx: JobContext) -> None:
    """Self-test job that always raises, to show failures are captured per job."""
    ctx.progress("about to fail", 0.5)
    raise RuntimeError(f"deliberate failure for target {ctx.target}")
