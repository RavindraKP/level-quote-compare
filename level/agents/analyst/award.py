"""Pinned scenarios and the award decision (Award page). No model calls here.

Awarding is allowed with prices still unconfirmed (buyers decide under uncertainty), but the
memo lists them with their rupee exposure on page one. Awarding drafts, never sends, the
clarification emails for every vendor that still has open questions.
"""

from __future__ import annotations

import sqlalchemy as sa

from level import db
from level.agents.analyst import memo
from level.agents.analyst.tools import load_scenario
from level.core import export as export_mod
from level.core import summary
from level.core.cells import load_snapshot
from level.pipeline import clarify


def scenarios(rfx_id: int, pinned_only: bool = False, limit: int = 50) -> list[dict]:
    """Saved scenarios, newest first, each with its headline figures."""
    query = sa.select(db.scenario).where(db.scenario.c.rfx_id == rfx_id)
    if pinned_only:
        query = query.where(db.scenario.c.pinned.is_(True))
    with db.connect() as conn:
        rows = conn.execute(query.order_by(db.scenario.c.id.desc()).limit(limit)).mappings().all()
    out = []
    for row in rows:
        _, result = load_scenario(row["id"])
        out.append({"id": row["id"], "name": row["name"], "pinned": row["pinned"],
                    "is_award": row["is_award"], "result": result,
                    "headline": summary.headline(result)})
    return out


def option_name(rfx_id: int, scenario_id: int) -> str:
    """An award option's name in words ("Cheapest per line, 3 vendors"), never its id."""
    from level.agents.analyst.tools import _vendors, scenario_name

    row, result = load_scenario(scenario_id)
    return row["name"] or scenario_name(result.params, _vendors(rfx_id))


def set_pinned(scenario_id: int, pinned: bool) -> None:
    with db.begin() as conn:
        conn.execute(db.scenario.update().where(db.scenario.c.id == scenario_id)
                     .values(pinned=pinned))


def award(rfx_id: int, scenario_id: int, actor: str = "buyer") -> list[int]:
    """Mark one scenario as the award, set the event to awarded, draft open questions.

    Returns the outbox ids of the clarification drafts written.
    """
    row, result = load_scenario(scenario_id)
    if row["rfx_id"] != rfx_id:
        raise ValueError("that scenario belongs to another event")
    with db.begin() as conn:
        conn.execute(db.scenario.update().where(db.scenario.c.rfx_id == rfx_id)
                     .values(is_award=False))
        conn.execute(db.scenario.update().where(db.scenario.c.id == scenario_id)
                     .values(is_award=True, pinned=True))
        before = conn.execute(sa.select(db.rfx.c.status).where(db.rfx.c.id == rfx_id)).scalar()
        conn.execute(db.rfx.update().where(db.rfx.c.id == rfx_id).values(status="awarded"))
        conn.execute(db.audit_event.insert().values(
            actor=actor, action="rfx.awarded", target_kind="scenario", target_id=scenario_id,
            before={"status": before},
            after={"status": "awarded", "scenario": row["name"],
                   "unconfirmed_prices": summary.risk(result)["unconfirmed_count"]}))
        vendor_ids = conn.execute(sa.select(db.invitation.c.vendor_id).where(
            db.invitation.c.rfx_id == rfx_id)).scalars().all()
        drafted = set(conn.execute(sa.select(db.message.c.vendor_id).where(
            db.message.c.rfx_id == rfx_id, db.message.c.direction == "out",
            db.message.c.status == "draft",
            db.message.c.subject.like(f"%{clarify.SUBJECT_TAIL}"))).scalars())
    # one clarification draft per vendor, not repeated if awarding twice
    return [mid for v in vendor_ids if v not in drafted
            if (mid := clarify.save_draft(rfx_id, v)) is not None]


def awarded(rfx_id: int) -> dict | None:
    return next((s for s in scenarios(rfx_id, pinned_only=True) if s["is_award"]), None)


def memo_docx(rfx_id: int, scenario_id: int) -> bytes:
    return memo.award_memo(rfx_id, scenario_id)


def scenario_xlsx(rfx_id: int, scenario_id: int) -> bytes:
    _, result = load_scenario(scenario_id)
    return export_mod.scenario_xlsx(result, load_snapshot(rfx_id))


def comparison_xlsx(rfx_id: int) -> bytes:
    return export_mod.comparison_xlsx(load_snapshot(rfx_id))


def drafts(rfx_id: int) -> list[dict]:
    """Outbox drafts (clarifications) waiting for the buyer to send."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            sa.select(db.message, db.vendor.c.name.label("vendor"))
            .join(db.vendor, db.vendor.c.id == db.message.c.vendor_id)
            .where(db.message.c.rfx_id == rfx_id, db.message.c.direction == "out",
                   db.message.c.status == "draft").order_by(db.message.c.id)).mappings()]
