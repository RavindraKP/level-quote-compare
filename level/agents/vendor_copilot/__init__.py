"""Vendor reply link (WP11): a vendor drops in their quote, answers only the questions the
checks raised about it, and confirms how it was read. See PLAN §8a.

    link.py       signed per-vendor token -> LinkScope(rfx_id, vendor_id)
    questions.py  what the vendor may see: the RFx, their open questions, their own reading
    answers.py    record_answer / confirm / correct: new claims and terms, cells rebuilt by code
    agent.py      the chat co-pilot (one tool: record_answer)
"""

from __future__ import annotations

import sqlalchemy as sa

from level import db, worker
from level.agents.vendor_copilot.link import LinkScope
from level.pipeline import run as pipeline_run
from level.types import Job


def submit_reply(scope: LinkScope, files: list[tuple[str, bytes]], body: str | None) -> int:
    """Store what the vendor dropped in and queue the normal pipeline job. Returns the job id."""
    body = (body or "").strip() or None
    if not files and not body:
        raise ValueError("Add a file or type your prices.")
    message_id = pipeline_run.ingest(scope.rfx_id, scope.vendor_id, files=files, body=body,
                                     channel="reply_link", subject="Reply via reply link")
    return pipeline_run.enqueue(message_id)


def reply_job(scope: LinkScope) -> Job | None:
    """The latest pipeline job for this vendor's latest reply, for the progress bar."""
    with db.connect() as conn:
        mid = conn.execute(sa.select(db.message.c.id).where(
            db.message.c.rfx_id == scope.rfx_id, db.message.c.vendor_id == scope.vendor_id,
            db.message.c.direction == "in").order_by(db.message.c.id.desc())).scalar()
        if mid is None:
            return None
        job_id = conn.execute(sa.select(db.job.c.id).where(
            db.job.c.kind == pipeline_run.JOB_KIND, db.job.c.target == str(mid))
            .order_by(db.job.c.id.desc())).scalar()
    return worker.get_job(job_id) if job_id else None
