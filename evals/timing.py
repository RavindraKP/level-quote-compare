"""Time to comparison and who closed each flag, read from the job and flag tables."""

from __future__ import annotations

from datetime import datetime


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    return (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds()


def score(snap: dict) -> dict:
    runs = [_seconds(j["created_at"], j["finished_at"]) for j in snap["jobs"]
            if j["kind"].startswith("pipeline") and j["status"] == "done"]
    runs = [r for r in runs if r is not None]
    flags = snap["flags"]
    closed_by = {who: sum(f["closed_by"] == who for f in flags)
                 for who in ("checks", "buyer", "vendor")}
    still_open = sum(f["state"] == "open" for f in flags)
    return {
        "replies_timed": len(runs),
        "minutes_to_grid_avg": round(sum(runs) / len(runs) / 60, 2) if runs else None,
        "minutes_to_grid_max": round(max(runs) / 60, 2) if runs else None,
        "flags_total": len(flags),
        "flags_open": still_open,
        "closed_by": closed_by,
        "human_touches": still_open + closed_by["buyer"],  # what a buyer had to look at
    }
