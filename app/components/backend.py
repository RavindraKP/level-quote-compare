"""The one file that connects the screens to the pipeline (WP2) and the core (WP3).

Bound to the real packages: the cell rebuild from level/core, the reading job from
level/pipeline, and clarification emails from level/pipeline/clarify (grouped by root cause
in code, so a draft never carries another vendor's data).
"""

from level.pipeline import clarify
from level.pipeline import run as _pipeline

# The worker job kind that reads one reply end to end.
PIPELINE_JOB_KIND = _pipeline.JOB_KIND


def rebuild_cells(rfx_id: int) -> None:
    """Rebuild the cells, then recompute grid-level checks (plausibility, assumed unit,
    unknown landed) on the new values. Flags a person closed stay closed."""
    _pipeline.post_checks(rfx_id)


def job_target(message_id: int) -> str:
    """Job target for one reply: the message id, exactly as level.pipeline.run enqueues it."""
    return str(message_id)


def save_draft(rfx_id: int, vendor_id: int) -> int | None:
    """Put one vendor's clarification email in the outbox as a draft. None if nothing to ask."""
    return clarify.save_draft(rfx_id, vendor_id)
