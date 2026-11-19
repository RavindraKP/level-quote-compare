"""Test stand-ins for WP2 (pipeline) and WP3 (cell rebuild); the app uses the real ones.

Kept for tests/test_ui_data.py, whose mock event has cells but no claims.

Honest by design:
- The mock pipeline job does not read anything. It walks the six stages so the Inbox
  progress can be seen, then opens a flag saying the reply was stored but not read.
  It never invents a price.
- The mock rebuild only re-derives existing cell rows: it replays each cell's conversion
  steps with the current assumptions, applies buyer resolutions, and sets the state in the
  PLAN §6 order. The real rebuild (level/core/cells.py) builds cells from claims.
"""

from __future__ import annotations

import json
import time
from decimal import ROUND_HALF_UP, Decimal

import sqlalchemy as sa

from level import db
from level.types import FLAG_CELL_EFFECT, CellState, Flag, FlagType
from level.worker import JobContext, handler

PIPELINE_JOB_KIND = "mock.pipeline"
STAGES = ["Intake", "Extract", "Match", "Normalise", "Verify", "Flag"]
MONEY = Decimal("0.0001")


@handler(PIPELINE_JOB_KIND)
def mock_pipeline(ctx: JobContext) -> None:
    """Walk the stages, then record that the reply still needs the real reader."""
    message_id = int(ctx.target.split(":")[1])
    for i, stage in enumerate(STAGES):
        ctx.progress(stage, (i + 1) / len(STAGES))
        time.sleep(1.5)
    with db.begin() as conn:
        msg = (
            conn.execute(sa.select(db.message).where(db.message.c.id == message_id))
            .mappings()
            .one()
        )
        already = conn.execute(
            sa.select(db.flag.c.id).where(
                db.flag.c.target_kind == "message",
                db.flag.c.target_id == message_id,
                db.flag.c.type == FlagType.UNREADABLE.value,
            )
        ).first()
        if not already:  # idempotent: a re-run does not add a second flag
            conn.execute(
                db.flag.insert().values(
                    rfx_id=msg["rfx_id"],
                    vendor_id=msg["vendor_id"],
                    target_kind="message",
                    target_id=message_id,
                    type=FlagType.UNREADABLE.value,
                    severity="warning",
                    reason="Stored, but not read: the reading pipeline is not connected yet.",
                    suggested_action="Re-run once the pipeline is installed.",
                )
            )


def apply_steps(steps: list[dict], fx: dict[str, Decimal]) -> tuple[list[dict], Decimal | None]:
    """Replay conversion steps; factors whose source is an FX key take the current rate."""
    out, value = [], None
    for s in steps:
        factor = fx.get(s.get("source") or "", Decimal(s["factor"]))
        start = Decimal(s["input"]) if value is None else value
        value = start * factor if s["operation"].startswith("multiply") else start / factor
        verb = "multiply" if s["operation"].startswith("multiply") else "divide"
        out.append(
            {
                **s,
                "input": str(start),
                "factor": str(factor),
                "operation": f"{verb} by {factor}",
                "output": str(value.quantize(MONEY)),
            }
        )
    return out, value.quantize(MONEY, ROUND_HALF_UP) if value is not None else None


def _state(flags: list[dict], has_assumptions: bool) -> tuple[CellState, str, str | None]:
    """PLAN §6 order: conflict > needs review > inferred > confirmed."""
    open_flags = [f for f in flags if f["state"] == "open"]
    effects = [(FLAG_CELL_EFFECT.get(FlagType(f["type"])), f) for f in open_flags]
    for wanted in (CellState.CONFLICT, CellState.NEEDS_REVIEW):
        hits = [f for e, f in effects if e == wanted]
        if hits:
            return wanted, hits[0]["reason"], None
    closers = {f["closed_by"] for f in flags if f["state"] == "closed"}
    confirmed_by = "vendor" if "vendor" in closers else "buyer" if "buyer" in closers else "checks"
    inferred = [f for e, f in effects if e == CellState.INFERRED]
    if inferred:
        return CellState.INFERRED, inferred[0]["reason"], None
    if has_assumptions:
        return CellState.INFERRED, "Uses a stated assumption (see receipt)", None
    reason = {
        "checks": "Passed all checks",
        "buyer": "Confirmed by the buyer",
        "vendor": "Confirmed by the vendor",
    }[confirmed_by]
    return CellState.CONFIRMED, reason, confirmed_by


def rebuild_cells(rfx_id: int) -> None:
    with db.begin() as conn:
        fx = {  # current exchange rates, keyed as conversion steps name them: "fx.USDINR"
            r["key"]: Decimal(r["value"])
            for r in conn.execute(sa.select(db.assumption)).mappings()
            if r["key"].startswith("fx.")
        }
        cells = conn.execute(sa.select(db.cell).where(db.cell.c.rfx_id == rfx_id)).mappings().all()
        for c in cells:
            flags = (
                conn.execute(
                    sa.select(db.flag).where(
                        db.flag.c.id.in_(c["flag_ids"] or [-1])
                        | (
                            (db.flag.c.vendor_id == c["vendor_id"])
                            & (db.flag.c.rfx_line_id == c["rfx_line_id"])
                        )
                    )
                )
                .mappings()
                .all()
            )
            meta = c["assumptions"] or {}
            steps, unit = apply_steps(meta.get("_base_steps", c["conversion_steps"] or []), fx)
            # Buyer resolutions, last one wins (stored as JSON in flag.resolution).
            for f in sorted(flags, key=lambda f: f["closed_at"] or ""):
                res = json.loads(f["resolution"]) if f["resolution"] else {}
                if res.get("action") == "edit_value" or (
                    res.get("action") == "apply_fix" and f["candidates"]
                ):
                    value = Decimal(res.get("value") or f["candidates"][0])
                    steps = steps + [
                        {
                            "label": "buyer correction",
                            "operation": "set by buyer",
                            "input": str(unit),
                            "factor": "1",
                            "output": str(value),
                            "source": f"flag {f['id']}",
                        }
                    ]
                    unit = value
                if res.get("action") == "not_quoted":
                    unit = None
            used = {k: v for k, v in meta.items() if not k.startswith("_")}
            if c["claim_id"] is None or unit is None:
                state, reason, by = CellState.NOT_QUOTED, "Marked not quoted", None
                landed = None
            else:
                state, reason, by = _state([dict(f) for f in flags], bool(used))
                freight = meta.get("_freight_per_unit")
                landed = (unit + Decimal(freight)) if freight is not None else None
            if "_base_steps" not in meta:  # remember the original chain for future replays
                meta = {**meta, "_base_steps": c["conversion_steps"] or []}
            conn.execute(
                db.cell.update()
                .where(db.cell.c.id == c["id"])
                .values(
                    state=state.value,
                    reason=reason,
                    confirmed_by=by,
                    unit_price_inr=str(unit) if unit is not None else None,
                    landed_price_inr=str(landed) if landed is not None else None,
                    conversion_steps=steps,
                    assumptions=meta,
                    updated_at=db.now_iso(),
                )
            )


def draft_clarification(f: Flag) -> tuple[str, str]:
    """A plain template from the flag's own reason. The real drafts come from WP2's clarify.py."""
    with db.connect() as conn:
        vendor = (
            conn.execute(sa.select(db.vendor).where(db.vendor.c.id == f.vendor_id))
            .mappings()
            .first()
        )
        line = None
        if f.rfx_line_id:
            line = (
                conn.execute(sa.select(db.rfx_line).where(db.rfx_line.c.id == f.rfx_line_id))
                .mappings()
                .first()
            )
    about = f" for line {line['line_no']} ({line['description']})" if line else ""
    subject = f"Clarification needed on your quote{about}"
    body = (
        f"Dear {vendor['name'] if vendor else 'supplier'},\n\n"
        f"Thank you for your quote. Before we can compare it, we need one clarification{about}:\n\n"
        f"  {f.reason}\n\n"
        "Could you confirm or correct this by reply?\n\nKind regards,\nProcurement team"
    )
    return subject, body
