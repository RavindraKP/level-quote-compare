"""Data access for the Inbox, Review, Compare and Overview screens.

Pages call these functions and only do layout. No Streamlit here, so it is testable.

Everything is read from and written to SQLite (the WP0 schema). The three things that belong
to other packages (rebuilding cells, the pipeline job kind, clarification drafts) go through
`components.backend`, which is the one file to change when WP2 and WP3 land.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import sqlalchemy as sa

from components import backend
from level import copy, db, sandbox, worker
from level.agents.analyst import audit
from level.agents.copilot import draft
from level.core.cells import in_totals
from level.types import (
    FLAG_CELL_EFFECT,
    AuditEntry,
    Cell,
    CellState,
    CheckResult,
    ClosedBy,
    ConversionStep,
    Evidence,
    Flag,
    FlagType,
    Job,
    QuoteClaim,
    Receipt,
    Severity,
)

# --- How each state looks. Colour is never the only signal: symbol + label too. -----------

STATE_SYMBOL = copy.STATE_SYMBOL
STATE_LABEL = copy.STATE_LABEL
STATE_COLOUR = {  # light backgrounds that keep dark text readable
    CellState.CONFIRMED: "#ffffff",
    CellState.INFERRED: "#eaf2fb",
    CellState.NEEDS_REVIEW: "#fff3cd",
    CellState.CONFLICT: "#f8d7da",
    CellState.NOT_QUOTED: "#eeeeee",
    CellState.AWAITING: "#f7f7f7",
}
# States whose price may be compared and awarded without a human looking first.
ELIGIBLE_STATES = {CellState.CONFIRMED, CellState.INFERRED}
VIEWS = {"as_quoted": "As quoted", "unit": "Per unit (INR)", "landed": "Landed (INR)"}
# The checks behind a price, named the way a buyer would describe them.
FLAG_TO_CHECK = {
    FlagType.EVIDENCE: "Found in the vendor's document",
    FlagType.READ_MISMATCH: "Second reading agrees",
    FlagType.ARITHMETIC: "Figures add up",
    FlagType.PLAUSIBILITY: "In line with other quotes",
    FlagType.SPEC_DEVIATION: "Matches the specification",
}
SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _rows(stmt) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def _audit(
    conn, action: str, target_kind: str, target_id, before=None, after=None, actor: str = "buyer"
) -> None:
    conn.execute(
        db.audit_event.insert().values(
            actor=actor,
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            before=before,
            after=after,
            at=db.now_iso(),
        )
    )


# --- The event, lines and vendors ---------------------------------------------------------


def current_rfx(rfx_id: int | None = None) -> dict | None:
    """The event the buyer is working on: the one picked in the sidebar if any, else the newest
    issued one that has replies, else the newest issued one, else the newest. A just-sent
    event with no replies yet is never chosen over one the buyer is working on (UX B10)."""
    rows = _working_events()
    picked = [r for r in rows if r["id"] == rfx_id]
    rows = [r for r in rows if not r["is_sandbox"]]  # a sandbox is only shown when picked
    issued = [r for r in rows if r["status"] != "draft"]
    with db.connect() as conn:
        replied = set(conn.execute(sa.select(db.message.c.rfx_id).where(
            db.message.c.direction == "in").distinct()).scalars())
    with_replies = [r for r in issued if r["id"] in replied]
    return (picked or with_replies or issued or rows or [None])[0]


def _working_events() -> list[dict]:
    """Every event except records of past awards (those appear on the RFx page only)."""
    records = draft.record_only_ids()
    return [r for r in _rows(sa.select(db.rfx).order_by(db.rfx.c.id.desc()))
            if r["id"] not in records]


def events() -> list[dict]:
    """Every issued event, then the sandboxes (drafts too), newest first (sidebar picker)."""
    rows = _working_events()
    return ([r for r in rows if r["status"] != "draft" and not r["is_sandbox"]]
            + [r for r in rows if r["is_sandbox"]])


def lines(rfx_id: int) -> list[dict]:
    return _rows(
        sa.select(db.rfx_line).where(db.rfx_line.c.rfx_id == rfx_id).order_by(db.rfx_line.c.line_no)
    )


def vendors(rfx_id: int) -> list[dict]:
    """Invited vendors (plus any vendor who replied uninvited), with reply status and verdict."""
    inv = _rows(
        sa.select(db.vendor, db.invitation.c.status.label("invitation_status"))
        .join(db.invitation, db.invitation.c.vendor_id == db.vendor.c.id)
        .where(db.invitation.c.rfx_id == rfx_id)
        .order_by(db.vendor.c.id)
    )
    replied_ids = {
        r["vendor_id"]
        for r in _rows(
            sa.select(db.message.c.vendor_id).where(
                db.message.c.rfx_id == rfx_id, db.message.c.direction == "in"
            )
        )
    }
    known = {v["id"] for v in inv}
    extra = [i for i in replied_ids if i and i not in known]
    if extra:
        inv += _rows(sa.select(db.vendor).where(db.vendor.c.id.in_(extra)))
    verdicts = gating_verdicts(rfx_id)
    for v in inv:
        v["replied"] = v["id"] in replied_ids
        v["verdict"] = verdicts.get(v["id"], "unclear") if v["replied"] else "awaiting"
    return inv


def all_vendors() -> list[dict]:
    return _rows(sa.select(db.vendor).order_by(db.vendor.c.name))


def gating_verdicts(rfx_id: int) -> dict[int, str]:
    """Per vendor: 'fail' if any gating answer fails, 'unclear' if any is missing or unclear."""
    gating = _rows(
        sa.select(db.question.c.id).where(db.question.c.rfx_id == rfx_id, db.question.c.is_gating)
    )
    gating_ids = {q["id"] for q in gating}
    answers = _rows(sa.select(db.answer).where(db.answer.c.rfx_id == rfx_id))
    by_vendor: dict[int, dict[int, str]] = {}
    for a in answers:
        if a["question_id"] in gating_ids:
            by_vendor.setdefault(a["vendor_id"], {})[a["question_id"]] = a["verdict"] or "unclear"
    out = {}
    for vendor_id, verdicts in by_vendor.items():
        values = [verdicts.get(q, "unclear") for q in gating_ids]
        out[vendor_id] = (
            "fail" if "fail" in values else "unclear" if "unclear" in values else "pass"
        )
    return out


# --- Cells and the grid -------------------------------------------------------------------


def _cell_from_row(r: dict) -> Cell:
    return Cell(
        rfx_id=r["rfx_id"],
        vendor_id=r["vendor_id"],
        rfx_line_id=r["rfx_line_id"],
        state=CellState(r["state"]),
        reason=r["reason"] or "",
        claim_id=r["claim_id"],
        as_quoted=r["as_quoted"],
        unit_price_inr=_dec(r["unit_price_inr"]),
        landed_price_inr=_dec(r["landed_price_inr"]),
        conversion_steps=[ConversionStep.model_validate(s) for s in (r["conversion_steps"] or [])],
        assumptions={
            k: str(v) for k, v in (r["assumptions"] or {}).items() if not k.startswith("_")
        },
        flag_ids=r["flag_ids"] or [],
        confirmed_by=ClosedBy(r["confirmed_by"]) if r["confirmed_by"] else None,
    )


@dataclass
class Grid:
    lines: list[dict]
    vendors: list[dict]
    cells: dict[tuple[int, int], Cell] = field(default_factory=dict)  # (vendor_id, line_id)


def read_vendor_ids(rfx_id: int) -> set[int]:
    """Vendors with at least one reply on this event that has been read (a reading of the
    email or of one of its files is stored, or prices are stored for them). The cell rebuild
    stores 'not quoted' cells for every vendor who replied, read or not; this tells the two
    apart for display."""
    msgs = sa.select(db.message.c.id).where(db.message.c.rfx_id == rfx_id,
                                            db.message.c.direction == "in")
    atts = sa.select(db.attachment.c.id).where(db.attachment.c.message_id.in_(msgs))
    runs = sa.select(db.extraction_run.c.message_id, db.extraction_run.c.attachment_id).where(
        sa.or_(db.extraction_run.c.message_id.in_(msgs),
               db.extraction_run.c.attachment_id.in_(atts)))
    run_rows = _rows(runs)
    msg_ids = {r["message_id"] for r in run_rows if r["message_id"]}
    att_ids = {r["attachment_id"] for r in run_rows if r["attachment_id"]}
    if att_ids:
        msg_ids |= {r["message_id"] for r in _rows(
            sa.select(db.attachment.c.message_id).where(db.attachment.c.id.in_(att_ids)))}
    read = {r["vendor_id"] for r in _rows(sa.select(db.quote_claim.c.vendor_id).where(
        db.quote_claim.c.rfx_id == rfx_id))}  # prices stored (read, typed or answered)
    if msg_ids:
        read |= {r["vendor_id"] for r in _rows(
            sa.select(db.message.c.vendor_id).where(db.message.c.id.in_(msg_ids)))}
    return read


def load_grid(rfx_id: int) -> Grid:
    """Every vendor x line ends in exactly one state (T6), including awaiting and not quoted."""
    ls, vs = lines(rfx_id), vendors(rfx_id)
    stored = {
        (r["vendor_id"], r["rfx_line_id"]): _cell_from_row(r)
        for r in _rows(sa.select(db.cell).where(db.cell.c.rfx_id == rfx_id))
    }
    grid = Grid(ls, vs)
    # A vendor counts as read only once a reply of theirs has been read: "not quoted" is for
    # a read reply with no price, never for a reply still waiting to be read.
    read_vendors = {vendor_id for vendor_id, _ in stored} & read_vendor_ids(rfx_id)
    for v in vs:
        for line in ls:
            key = (v["id"], line["id"])
            if not v["replied"] or v["id"] not in read_vendors:
                reason = (
                    "Vendor has not replied yet"
                    if not v["replied"]
                    else "Reply received but not read yet"
                )
                grid.cells[key] = Cell(
                    rfx_id=rfx_id,
                    vendor_id=v["id"],
                    rfx_line_id=line["id"],
                    state=CellState.AWAITING,
                    reason=reason,
                )
            elif key in stored:
                grid.cells[key] = stored[key]
            else:
                grid.cells[key] = Cell(
                    rfx_id=rfx_id,
                    vendor_id=v["id"],
                    rfx_line_id=line["id"],
                    state=CellState.NOT_QUOTED,
                    reason="No price found for this line in the reply",
                )
    return grid


def cell_value(c: Cell, view: str) -> Decimal | None:
    """The comparable number for a view. As-quoted has none: units differ between vendors."""
    if c.state in (CellState.NOT_QUOTED, CellState.AWAITING):
        return None
    return {"unit": c.unit_price_inr, "landed": c.landed_price_inr}.get(view)


def cell_text(c: Cell, view: str, rank: int | None = None) -> str:
    """Text shown in a grid cell: status symbol, the value, and the L1/L2/L3 rank tag."""
    symbol = STATE_SYMBOL[c.state]
    if c.state == CellState.NOT_QUOTED:
        return f"{symbol} not quoted"
    if c.state == CellState.AWAITING:
        return f"{symbol} awaiting reply"
    if view == "as_quoted":
        text = c.as_quoted or "?"
    else:
        value = cell_value(c, view)
        text = copy.unit_price(value).lstrip("₹") if value is not None else "unknown"
    return f"{symbol} {text}" + (f"  L{rank}" if rank else "")


def ranks_by_line(grid: Grid, view: str) -> dict[tuple[int, int], int]:
    """(vendor id, line id) -> 1, 2 or 3: the L1/L2/L3 order among eligible prices.

    Eligible: a verified or assumed price from a vendor that meets the requirements (an
    unclear verdict is not "meets", QA M15). Equal prices share a rank. Only the first three
    places are tagged.
    """
    failed = {v["id"] for v in grid.vendors if v["verdict"] != "pass"}
    by_line: dict[int, list[tuple[Decimal, int]]] = {}
    for (vendor_id, line_id), c in grid.cells.items():
        value = cell_value(c, view)
        if value is None or c.state not in ELIGIBLE_STATES or vendor_id in failed:
            continue
        by_line.setdefault(line_id, []).append((value, vendor_id))
    out = {}
    for line_id, prices in by_line.items():
        distinct = sorted({value for value, _ in prices})
        for value, vendor_id in prices:
            rank = distinct.index(value) + 1
            if rank <= 3:
                out[(vendor_id, line_id)] = rank
    return out


def lowest_by_line(grid: Grid, view: str) -> dict[int, int]:
    """Line id -> vendor id with the lowest eligible price (the L1)."""
    return {line_id: vendor_id for (vendor_id, line_id), rank in ranks_by_line(grid, view).items()
            if rank == 1}


def vendor_summary(grid: Grid, view: str) -> dict[int, dict]:
    """Per vendor: lines with a price, lines total, and the annual value of priced lines.

    The total covers every line with a price in this view, confirmed or not, so the page
    labels it as such. It is None for the as-quoted view, where units are not comparable.
    """
    qty = {line["id"]: _dec(line["annual_qty"]) for line in grid.lines}
    out = {}
    for v in grid.vendors:
        quoted, total, held, priced = 0, Decimal(0), 0, 0
        for line in grid.lines:
            c = grid.cells[(v["id"], line["id"])]
            if c.state in (CellState.NOT_QUOTED, CellState.AWAITING):
                continue
            quoted += 1
            if not in_totals(c):  # a likely unit slip is not added at face value
                held += 1
                continue
            value = cell_value(c, view)
            if value is not None and qty[line["id"]] is not None:
                total += value * qty[line["id"]]
                priced += 1
        out[v["id"]] = {
            "quoted": quoted,
            "lines": len(grid.lines),
            "held_back": held,
            "priced": priced,  # lines inside the total (QA pass 3 H7)
            "total": total if view != "as_quoted" and priced else None,
        }
    return out


# --- Receipts -----------------------------------------------------------------------------


def _flag_from_row(r: dict) -> Flag:
    return Flag(
        id=r["id"],
        rfx_id=r["rfx_id"],
        vendor_id=r["vendor_id"],
        target_kind=r["target_kind"],
        target_id=r["target_id"],
        rfx_line_id=r["rfx_line_id"],
        type=FlagType(r["type"]),
        severity=Severity(r["severity"]),
        reason=r["reason"],
        suggested_action=r["suggested_action"],
        candidates=r["candidates"] or [],
        state=r["state"],
        closed_by=ClosedBy(r["closed_by"]) if r["closed_by"] else None,
        resolution=r["resolution"],
    )


def claim(claim_id: int | None) -> QuoteClaim | None:
    if claim_id is None:
        return None
    rows = _rows(sa.select(db.quote_claim).where(db.quote_claim.c.id == claim_id))
    if not rows:
        return None
    r = rows[0]
    return QuoteClaim(
        id=r["id"],
        rfx_id=r["rfx_id"],
        vendor_id=r["vendor_id"],
        run_id=r["run_id"],
        vendor_item_text=r["vendor_item_text"] or "",
        raw_price=r["raw_price"],
        raw_currency=r["raw_currency"],
        raw_uom_text=r["raw_uom_text"],
        basis=r["basis"],
        quantity_break=r["quantity_break"],
        scope=r["scope"],
        rule_text=r["rule_text"],
        read_candidates=r["read_candidates"] or [],
        evidence=Evidence(
            attachment_id=r["attachment_id"],
            locator=r["evidence_locator"] or "",
            quote=r["evidence_quote"] or "",
            verified=r["evidence_ok"],
        ),
    )


def cell_flags(c: Cell) -> list[Flag]:
    """Flags listed on the cell, plus any flag aimed at this vendor x line or its claim."""
    cond = (db.flag.c.vendor_id == c.vendor_id) & (db.flag.c.rfx_line_id == c.rfx_line_id)
    if c.flag_ids:
        cond = cond | db.flag.c.id.in_(c.flag_ids)
    if c.claim_id:
        cond = cond | ((db.flag.c.target_kind == "claim") & (db.flag.c.target_id == c.claim_id))
    return [_flag_from_row(r) for r in _rows(sa.select(db.flag).where(cond).order_by(db.flag.c.id))]


def _checks(c: Cell, q: QuoteClaim | None, flags: list[Flag]) -> list[CheckResult]:
    """Which checks passed or failed for this cell, read from evidence_ok and the flags."""
    if q is None:
        return []
    ev = q.evidence.verified
    checks = [
        CheckResult(
            name=FLAG_TO_CHECK[FlagType.EVIDENCE],
            passed=bool(ev),
            detail={
                True: "Quoted words found in the source",
                False: "Quoted words not found",
                None: "Not checked (no text layer, e.g. a photo)",
            }[ev],
        )
    ]
    for ftype, name in FLAG_TO_CHECK.items():
        if ftype == FlagType.EVIDENCE:
            continue
        hits = [f for f in flags if f.type == ftype]
        if hits:
            f = hits[-1]
            closed = (
                f" (settled by {copy.CLOSED_BY[f.closed_by.value]})"
                if f.state == "closed" and f.closed_by else ""
            )
            checks.append(CheckResult(name=name, passed=False,
                                      detail=copy.plain(f.reason) + closed))
        else:
            checks.append(CheckResult(name=name, passed=True, detail="No issue raised"))
    return checks


def _history(flags: list[Flag], c: Cell) -> list[AuditEntry]:
    flag_ids = [f.id for f in flags]
    cond = (db.audit_event.c.target_kind == "flag") & db.audit_event.c.target_id.in_(
        flag_ids or [-1]
    )
    if c.claim_id:
        cond = cond | (
            (db.audit_event.c.target_kind == "claim") & (db.audit_event.c.target_id == c.claim_id)
        )
    cond = cond | (db.audit_event.c.target_kind == "assumption")
    rows = _rows(sa.select(db.audit_event).where(cond).order_by(db.audit_event.c.id))
    return [
        AuditEntry(
            actor=r["actor"], action=r["action"], before=r["before"], after=r["after"], at=r["at"]
        )
        for r in rows
    ]


def receipt(grid: Grid, vendor_id: int, line_id: int) -> Receipt:
    c = grid.cells[(vendor_id, line_id)]
    q = claim(c.claim_id)
    flags = cell_flags(c) if c.state != CellState.AWAITING else []
    return Receipt(
        cell=c,
        claim=q,
        evidence=q.evidence if q else None,
        conversion_steps=c.conversion_steps,
        assumptions_used=c.assumptions,
        checks=_checks(c, q, flags),
        flags=flags,
        history=_history(flags, c),
    )


def attachment(attachment_id: int | None) -> dict | None:
    if attachment_id is None:
        return None
    rows = _rows(sa.select(db.attachment).where(db.attachment.c.id == attachment_id))
    return rows[0] if rows else None


def message_for_attachment(attachment_id: int | None) -> dict | None:
    att = attachment(attachment_id)
    if not att or not att["message_id"]:
        return None
    rows = _rows(sa.select(db.message).where(db.message.c.id == att["message_id"]))
    return rows[0] if rows else None


# --- Review queue -------------------------------------------------------------------------


def open_flags(rfx_id: int) -> list[Flag]:
    """Open flags, most serious first, then by vendor."""
    rows = _rows(sa.select(db.flag).where(db.flag.c.rfx_id == rfx_id, db.flag.c.state == "open"))
    flags = [_flag_from_row(r) for r in rows]
    return sorted(flags, key=lambda f: (SEVERITY_ORDER[f.severity], f.vendor_id or 0, f.id or 0))


def flag_evidence(f: Flag) -> Evidence | None:
    """The receipt anchor behind a flag: its claim, term or answer evidence."""
    table = {"claim": db.quote_claim, "term": db.vendor_term, "answer": db.answer}.get(
        f.target_kind
    )
    if table is not None and f.target_id:
        rows = _rows(sa.select(table).where(table.c.id == f.target_id))
        if rows:
            r = rows[0]
            return Evidence(
                attachment_id=r["attachment_id"],
                locator=r["evidence_locator"] or "",
                quote=r["evidence_quote"] or "",
                verified=r["evidence_ok"],
            )
    if f.target_kind == "attachment" and f.target_id:
        return Evidence(attachment_id=f.target_id, locator="", quote="")
    return None


def flag_target_summary(f: Flag) -> dict:
    """What the flag is about, for the right-hand column of the review screen."""
    out: dict = {}
    if f.target_kind == "claim" and f.target_id:
        q = claim(f.target_id)
        if q:
            out = {
                "Item as written": q.vendor_item_text,
                "Price as written": q.raw_price,
                "Currency": q.raw_currency,
                "Unit as written": q.raw_uom_text,
            }
    elif f.target_kind in ("term", "answer") and f.target_id:
        table = db.vendor_term if f.target_kind == "term" else db.answer
        r = _rows(sa.select(table).where(table.c.id == f.target_id))[0]
        out = {
            k: r.get(k)
            for k in ("type", "text", "value", "conditions", "verdict", "rationale")
            if r.get(k) is not None
        }
    if f.rfx_line_id:
        line = _rows(sa.select(db.rfx_line).where(db.rfx_line.c.id == f.rfx_line_id))[0]
        out["RFx line"] = f"{line['line_no']}. {line['description']} (per {line['canonical_uom']})"
    return out


REVIEW_ACTIONS = copy.REVIEW_ACTIONS


def resolve_flag(
    flag_id: int,
    action: str,
    value: str | None = None,
    note: str | None = None,
    actor: str = "buyer",
) -> None:
    """Close a flag with the buyer's decision, log it, and rebuild the cells.

    The decision is stored as JSON in flag.resolution: {"action", "value", "note"}. The cell
    rebuild (WP3) reads it; nothing here edits a cell or a claim directly.
    """
    with db.begin() as conn:
        rfx_id = _close_flag(conn, flag_id, action, value, note, actor)
    backend.rebuild_cells(rfx_id)


def resolve_flags(flag_ids: list[int], action: str, actor: str = "buyer") -> None:
    """The same decision on several issues at once (Review "Accept all"): each flag is closed
    and logged exactly as resolve_flag does, in one transaction, then one rebuild. The audit
    entries share a batch number so Undo can take the whole decision back."""
    if not flag_ids:
        return
    with db.begin() as conn:
        batch = (conn.execute(sa.select(sa.func.max(db.audit_event.c.id))).scalar() or 0) + 1
        for flag_id in flag_ids:
            rfx_id = _close_flag(conn, flag_id, action, None, None, actor, batch=batch)
    backend.rebuild_cells(rfx_id)


def _close_flag(conn, flag_id: int, action: str, value: str | None, note: str | None,
                actor: str, batch: int | None = None) -> int:
    """Close one flag with a decision and write its audit entry. Returns the flag's RFx id."""
    if action not in REVIEW_ACTIONS:
        raise ValueError(f"unknown review action: {action}")
    if action == "edit_value":
        if _dec(value) is None:
            raise ValueError("Enter the corrected price as a number, e.g. 12.50")
        value = str(_dec(value))
    resolution = {"action": action, "value": value, "note": note}
    before = dict(conn.execute(sa.select(db.flag).where(db.flag.c.id == flag_id)).mappings().one())
    conn.execute(db.flag.update().where(db.flag.c.id == flag_id).values(
        state="closed", closed_by=actor, resolution=json.dumps(resolution),
        closed_at=db.now_iso()))
    _audit(conn, f"flag.{action}", "flag", flag_id,
           before={"state": before["state"], "resolution": before["resolution"]},
           after={**resolution, "batch": batch} if batch else resolution, actor=actor)
    return before["rfx_id"]


DECISIONS = tuple(f"flag.{a}" for a in REVIEW_ACTIONS)


def last_decision(rfx_id: int) -> dict | None:
    """The buyer's most recent Review decision on this event, if it can still be undone:
    {"action", "flag_ids", "value"}. None if the latest Review entry is already an undo, or
    if any of its issues has since been reopened or changed by someone else."""
    rows = _rows(sa.select(db.audit_event).where(
        db.audit_event.c.actor == "buyer", db.audit_event.c.target_kind == "flag",
        db.audit_event.c.action.in_((*DECISIONS, "flag.undo")))
        .order_by(db.audit_event.c.id.desc()).limit(200))
    flags = {r["id"]: r for r in _rows(sa.select(db.flag).where(db.flag.c.rfx_id == rfx_id))}
    rows = [r for r in rows if r["target_id"] in flags]  # this event only
    if not rows or rows[0]["action"] == "flag.undo":
        return None
    batch = (rows[0]["after"] or {}).get("batch")
    picked = [r for r in rows if batch and (r["after"] or {}).get("batch") == batch] or rows[:1]
    for r in picked:  # still closed by the buyer with this very decision
        f = flags[r["target_id"]]
        decided = {k: v for k, v in (r["after"] or {}).items() if k != "batch"}
        if f["state"] != "closed" or f["closed_by"] != "buyer" or \
                json.loads(f["resolution"] or "{}") != decided:
            return None
    return {"action": rows[0]["action"].removeprefix("flag."),
            "flag_ids": [r["target_id"] for r in picked],
            "value": (rows[0]["after"] or {}).get("value")}


def undo_last(rfx_id: int) -> list[int]:
    """Reopen the issues of the last decision, log it, and rebuild. Returns the flag ids.

    The rebuild then treats them as never decided: a price issue raised by the grid checks
    is raised again by those checks if it still applies."""
    last = last_decision(rfx_id)
    if last is None:
        return []
    with db.begin() as conn:
        for flag_id in last["flag_ids"]:
            before = conn.execute(sa.select(db.flag.c.resolution).where(
                db.flag.c.id == flag_id)).scalar()
            conn.execute(db.flag.update().where(db.flag.c.id == flag_id).values(
                state="open", closed_by=None, resolution=None, closed_at=None))
            _audit(conn, "flag.undo", "flag", flag_id, before={"resolution": before},
                   after={"state": "open"})
    backend.rebuild_cells(rfx_id)
    return last["flag_ids"]


def send_outbox(
    rfx_id: int, vendor_id: int, subject: str, body: str, flag_id: int | None = None
) -> int:
    """Store an outgoing email (sending is simulated) and log it. Returns the message id."""
    vendor = _rows(sa.select(db.vendor).where(db.vendor.c.id == vendor_id))[0]
    with db.begin() as conn:
        msg_id = conn.execute(
            db.message.insert().values(
                rfx_id=rfx_id,
                vendor_id=vendor_id,
                direction="out",
                channel="email",
                status="sent",
                subject=subject,
                recipient=vendor["email"],
                body=body,
                received_at=db.now_iso(),
            )
        ).inserted_primary_key[0]
        _audit(
            conn,
            "clarification.sent",
            "flag" if flag_id else "message",
            flag_id or msg_id,
            after={"message_id": msg_id, "subject": subject},
        )
    return msg_id


# --- Assumptions --------------------------------------------------------------------------


def assumptions() -> list[dict]:
    return _rows(sa.select(db.assumption).order_by(db.assumption.c.key))


def set_assumption(
    key: str, value: str, source: str | None, as_of: str | None, rfx_id: int | None
) -> None:
    """Change one global input (T7), log it, and recompute every cell."""
    if _dec(value) is None:
        raise ValueError(f"{key} must be a number")
    if sandbox.is_sandbox(rfx_id):  # shared by every event: a sandbox must not change them
        raise sandbox.Limit("Assumptions are shared with every event and cannot be changed "
                            "from the sandbox.")
    with db.begin() as conn:
        old = (
            conn.execute(sa.select(db.assumption).where(db.assumption.c.key == key))
            .mappings()
            .first()
        )
        values = {
            "value": str(_dec(value)),
            "source": source,
            "as_of": as_of,
            "updated_at": db.now_iso(),
        }
        if old:
            conn.execute(db.assumption.update().where(db.assumption.c.key == key).values(**values))
        else:
            conn.execute(db.assumption.insert().values(key=key, **values))
        _audit(
            conn,
            "assumption.set",
            "assumption",
            None,
            before=dict(old) if old else None,
            after={"key": key, **values},
        )
    if rfx_id:
        backend.rebuild_cells(rfx_id)


# --- Questionnaire and documents ----------------------------------------------------------


def questionnaire(rfx_id: int) -> tuple[list[dict], dict[tuple[int, int], dict]]:
    """(questions, answers by (question_id, vendor_id))."""
    qs = _rows(
        sa.select(db.question)
        .where(db.question.c.rfx_id == rfx_id)
        .order_by(db.question.c.q_no, db.question.c.id)
    )
    answers = {
        (a["question_id"], a["vendor_id"]): a
        for a in _rows(sa.select(db.answer).where(db.answer.c.rfx_id == rfx_id))
    }
    return qs, answers


def documents(rfx_id: int) -> list[dict]:
    """Every inbound attachment for this event, with its vendor."""
    return _rows(
        sa.select(db.attachment, db.vendor.c.name.label("vendor_name"), db.message.c.received_at)
        .join(db.message, db.message.c.id == db.attachment.c.message_id)
        .join(db.vendor, db.vendor.c.id == db.attachment.c.vendor_id, isouter=True)
        .where(db.message.c.rfx_id == rfx_id, db.message.c.direction == "in")
        .order_by(db.attachment.c.id)
    )


# --- Inbox --------------------------------------------------------------------------------


def job_targets(message_id: int) -> list[str]:
    """Job targets for one reply: the message itself, as the backend enqueues it."""
    return [backend.job_target(message_id)]


def jobs_for_message(message_id: int) -> list[Job]:
    rows = _rows(
        sa.select(db.job)
        .where(
            db.job.c.kind == backend.PIPELINE_JOB_KIND, db.job.c.target.in_(job_targets(message_id))
        )
        .order_by(db.job.c.id)
    )
    return [Job.model_validate(r) for r in rows]


def messages(rfx_id: int, direction: str) -> list[dict]:
    rows = _rows(
        sa.select(db.message, db.vendor.c.name.label("vendor_name"))
        .join(db.vendor, db.vendor.c.id == db.message.c.vendor_id, isouter=True)
        .where(db.message.c.rfx_id == rfx_id, db.message.c.direction == direction)
        .order_by(db.message.c.id.desc())
    )
    for m in rows:
        m["attachments"] = _rows(
            sa.select(db.attachment).where(db.attachment.c.message_id == m["id"])
        )
    return rows


def reply_findings(rfx_id: int, message: dict) -> dict:
    """What came out of one reply: claims read, and the vendor's cells and open flags."""
    att_ids = [a["id"] for a in message["attachments"]]
    runs = sa.select(db.extraction_run.c.id).where(
        db.extraction_run.c.message_id == message["id"])  # email-body claims have no file
    n_claims = len(
        _rows(
            sa.select(db.quote_claim.c.id).where(
                db.quote_claim.c.rfx_id == rfx_id,
                sa.or_(db.quote_claim.c.attachment_id.in_(att_ids or [-1]),
                       db.quote_claim.c.run_id.in_(runs)),
            )
        )
    )
    cells = _rows(
        sa.select(db.cell.c.state).where(
            db.cell.c.rfx_id == rfx_id, db.cell.c.vendor_id == message["vendor_id"]
        )
    )
    states: dict[str, int] = {}
    for c in cells:
        states[c["state"]] = states.get(c["state"], 0) + 1
    flags = _rows(
        sa.select(db.flag.c.id).where(
            db.flag.c.rfx_id == rfx_id,
            db.flag.c.vendor_id == message["vendor_id"],
            db.flag.c.state == "open",
        )
    )
    return {"claims": n_claims, "states": states, "open_flags": len(flags)}


@dataclass
class Upload:
    filename: str
    data: bytes
    mime: str | None = None


def _store_file(upload: Upload) -> tuple[str, Path]:
    """Layer 1: keep the original untouched, named by its SHA-256 (extension kept for viewers)."""
    sha = hashlib.sha256(upload.data).hexdigest()
    path = db.upload_dir() / f"{sha}{Path(upload.filename).suffix.lower()}"
    if not path.exists():
        path.write_bytes(upload.data)
    return sha, path


def add_reply(
    rfx_id: int,
    vendor_id: int | None,
    new_vendor_name: str | None,
    files: list[Upload],
    body: str | None,
) -> list[int]:
    """Store a reply (files and/or pasted text) and queue it for reading. Returns job ids.

    Same vendor + same content twice (a double-click) reuses the stored reply, and the worker's
    dedupe key returns the existing job instead of starting a second run.
    """
    body = (body or "").strip()
    if not files and not body:
        raise ValueError("Add a file or paste the reply text.")
    sandbox.check_upload(rfx_id, [len(f.data) for f in files])  # caps API use in a sandbox
    with db.begin() as conn:
        if vendor_id is None:
            name = (new_vendor_name or "").strip()
            if not name:
                raise ValueError("Pick a vendor or type a new vendor's name.")
            vendor_id = conn.execute(db.vendor.insert().values(name=name)).inserted_primary_key[0]
            conn.execute(
                db.invitation.insert().values(rfx_id=rfx_id, vendor_id=vendor_id, status="replied")
            )
        stored = [(_store_file(f), f) for f in files]
        content_hash = hashlib.sha256(
            (body + "|" + "|".join(sha for (sha, _), _ in stored)).encode()
        ).hexdigest()
        subject = f"Reply added [{content_hash[:12]}]"
        existing = conn.execute(
            sa.select(db.message.c.id).where(
                db.message.c.rfx_id == rfx_id,
                db.message.c.vendor_id == vendor_id,
                db.message.c.subject == subject,
            )
        ).scalar()
        if existing:
            message_id = existing
        else:
            message_id = conn.execute(
                db.message.insert().values(
                    rfx_id=rfx_id,
                    vendor_id=vendor_id,
                    direction="in",
                    channel="upload",
                    status="received",
                    subject=subject,
                    body=body or None,
                    received_at=db.now_iso(),
                )
            ).inserted_primary_key[0]
            for (sha, path), f in stored:
                conn.execute(
                    db.attachment.insert().values(
                        message_id=message_id,
                        vendor_id=vendor_id,
                        filename=f.filename,
                        mime=f.mime,
                        kind="quote",
                        path=str(path),
                        sha256=sha,
                        size_bytes=len(f.data),
                    )
                )
            conn.execute(
                db.invitation.update()
                .where(db.invitation.c.rfx_id == rfx_id, db.invitation.c.vendor_id == vendor_id)
                .values(status="replied")
            )
            _audit(
                conn,
                "reply.added",
                "message",
                message_id,
                after={"files": [f.filename for f in files], "has_body": bool(body)},
            )
    return [
        worker.enqueue(
            backend.PIPELINE_JOB_KIND, backend.job_target(message_id), file_hash=content_hash
        )
    ]


def retry_message(message_id: int) -> list[int]:
    """Queue a fresh run for a reply whose last run failed."""
    with db.connect() as conn:
        rfx_id = conn.execute(sa.select(db.message.c.rfx_id).where(
            db.message.c.id == message_id)).scalar()
    sandbox.check_read(rfx_id)
    return [
        worker.enqueue(
            backend.PIPELINE_JOB_KIND,
            backend.job_target(message_id),
            file_hash=f"retry-{db.now_iso()}",
        )
    ]


# --- Overview counters --------------------------------------------------------------------


def _parse_ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def minutes_to_grid(message: dict, jobs: list[Job]) -> float | None:
    """Minutes from a reply reaching Level to its first successful read finishing.

    Arrival is the later of the email's own date and when Level stored it: a seeded or
    forwarded email can carry a date long before it reached us.
    """
    stamps = [_parse_ts(message.get(k)) for k in ("received_at", "created_at")]
    stamps = [t for t in stamps if t]
    finished = [_parse_ts(j.finished_at) for j in jobs if j.status == "done"]
    finished = [f for f in finished if f]
    if not stamps or not finished:
        return None
    return round((min(finished) - max(stamps)).total_seconds() / 60, 1)


def overview(rfx_id: int) -> dict:
    vs = vendors(rfx_id)
    replies = messages(rfx_id, "in")
    read, timings = 0, []
    for m in replies:
        jobs = jobs_for_message(m["id"])
        if jobs and jobs[-1].status == "done":
            read += 1
        mins = minutes_to_grid(m, jobs)
        if mins is not None:
            timings.append({"vendor": m["vendor_name"], "minutes": mins})
    flags = _rows(sa.select(db.flag.c.state, db.flag.c.closed_by).where(db.flag.c.rfx_id == rfx_id))
    closed_by = {k.value: 0 for k in ClosedBy}
    for f in flags:
        if f["state"] == "closed" and f["closed_by"] in closed_by:
            closed_by[f["closed_by"]] += 1
    open_count = sum(1 for f in flags if f["state"] == "open")
    return {
        "invited": len(vs),
        "replied": sum(1 for v in vs if v["replied"]),
        "replies": len(replies),
        "replies_read": read,
        "timings": timings,
        "open_flags": open_count,
        "needed_human": open_count + closed_by["buyer"],
        "closed_by": closed_by,
    }


def flag_effect(f: Flag) -> str:
    effect = FLAG_CELL_EFFECT.get(f.type)
    return STATE_LABEL[effect] if effect else "No effect on prices"


def format_inr(value: Decimal | None, decimals: int = 0) -> str:
    """Indian digit grouping: 7538000 -> '₹75,38,000'. Display only; no arithmetic."""
    return copy.inr(value, decimals)


def format_inr_short(value: Decimal | None) -> str:
    """Compact rupees for narrow cells: '₹75.4 L', '₹3.05 Cr'. Display rounding only."""
    return copy.inr_short(value)


# --- Event header, response tracker, supplier card (WP12) ---------------------------------


def open_issue_count(rfx_id: int) -> int:
    """Open points to check on this event (the badge on Review, the Award warning). Counted
    from the Review queue itself, so a point folded into another (one cause, one point) is
    not counted twice and the badge always matches "N left" on Review."""
    from components import review_data  # imported here: review_data imports this module

    return sum(1 for i in review_data.issues(rfx_id) if not i.decided)


def tracker(rfx_id: int) -> list[dict]:
    """One row per invited vendor: status, when the reply came, lines quoted, open issues,
    and the questionnaire result. Counts only; no prices."""
    grid = load_grid(rfx_id)
    replies = messages(rfx_id, "in")
    issues = _rows(sa.select(db.flag.c.vendor_id).where(db.flag.c.rfx_id == rfx_id,
                                                        db.flag.c.state == "open"))
    rows = []
    for v in grid.vendors:
        theirs = [m for m in replies if m["vendor_id"] == v["id"]]
        quoted = sum(1 for line in grid.lines if grid.cells[(v["id"], line["id"])].state
                     not in (CellState.NOT_QUOTED, CellState.AWAITING))
        status = ("Replied" if v["replied"] else
                  "No response" if v.get("invitation_status") == "no_response" else "Invited")
        rows.append({
            "vendor_id": v["id"],
            "Vendor": v["name"] + (" (current supplier)" if v.get("is_incumbent") else ""),
            "Status": status,
            "Received": copy.date_time(max((m["received_at"] or "" for m in theirs),
                                           default=None)) if theirs else "—",
            "Lines quoted": f"{quoted}/{len(grid.lines)}" if v["replied"] else "—",
            "Open issues": sum(1 for f in issues if f["vendor_id"] == v["id"]),
            "Requirements": copy.VERDICT_LABEL[v["verdict"]],
        })
    return rows


def supplier_card(rfx_id: int, vendor_id: int) -> dict:
    """Name, current-supplier badge, dated questionnaire answers (certificates), and what
    the vendor supplied under last year's contract."""
    v = _rows(sa.select(db.vendor).where(db.vendor.c.id == vendor_id))[0]
    questions, answers = questionnaire(rfx_id)
    dated = [{"Question": q["text"], "Answer": answers[(q["id"], vendor_id)]["value"] or "—",
              "Result": copy.VERDICT_LABEL.get(answers[(q["id"], vendor_id)]["verdict"] or
                                               "unclear", "Unclear")}
             for q in questions if q["answer_type"] == "date" and (q["id"], vendor_id) in answers]
    history = _rows(sa.select(db.price_history.c.period, db.price_history.c.source)
                    .where(db.price_history.c.vendor_id == vendor_id))
    contract = [h for h in history if h["source"] == "contract"]
    return {
        "name": v["name"],
        "email": v["email"],
        "incumbent": bool(v["is_incumbent"]),
        "certificates": dated,
        "contract_lines": len(contract),
        "periods": sorted({h["period"] for h in contract if h["period"]}),
        "quoted_last_year": len(history) - len(contract),
    }


def flag_stakes(rfx_id: int) -> dict[int, Decimal]:
    """Flag id -> annual value of its line (quantity x the price we have). Used only to put
    the review queue in order of money at stake; the figure itself is never shown."""
    qty = {line["id"]: _dec(line["annual_qty"]) for line in lines(rfx_id)}
    price = {(r["vendor_id"], r["rfx_line_id"]): _dec(r["unit_price_inr"])
             for r in _rows(sa.select(db.cell).where(db.cell.c.rfx_id == rfx_id))}
    out = {}
    for f in _rows(sa.select(db.flag).where(db.flag.c.rfx_id == rfx_id,
                                            db.flag.c.state == "open")):
        q, p = qty.get(f["rfx_line_id"]), price.get((f["vendor_id"], f["rfx_line_id"]))
        out[f["id"]] = q * p if q is not None and p is not None else Decimal(0)
    return out


def review_queue(rfx_id: int) -> list[Flag]:
    """Open issues grouped by vendor (vendor with most money at stake first), and within a
    vendor by severity, then money at stake."""
    stakes = flag_stakes(rfx_id)
    flags = open_flags(rfx_id)
    vendor_stake: dict[int, Decimal] = {}
    for f in flags:
        vendor_stake[f.vendor_id or 0] = vendor_stake.get(f.vendor_id or 0, 0) + stakes[f.id]
    return sorted(flags, key=lambda f: (-vendor_stake[f.vendor_id or 0], f.vendor_id or 0,
                                        SEVERITY_ORDER[f.severity], -stakes[f.id], f.id))


def outbox_drafts(rfx_id: int, vendor_id: int) -> list[dict]:
    """Unsent clarification drafts for one vendor, newest first."""
    return _rows(sa.select(db.message).where(
        db.message.c.rfx_id == rfx_id, db.message.c.vendor_id == vendor_id,
        db.message.c.direction == "out", db.message.c.status == "draft")
        .order_by(db.message.c.id.desc()))


def draft_for_vendor(rfx_id: int, vendor_id: int) -> int | None:
    """Write the clarification email for one vendor into the outbox as a draft."""
    return backend.save_draft(rfx_id, vendor_id)


def send_draft(message_id: int, subject: str, body: str) -> None:
    """Send an outbox draft as edited by the buyer (sending is simulated) and log it."""
    with db.begin() as conn:
        conn.execute(db.message.update().where(db.message.c.id == message_id).values(
            subject=subject, body=body, status="sent", recipient=sa.select(db.vendor.c.email)
            .where(db.vendor.c.id == db.message.c.vendor_id).scalar_subquery(),
            received_at=db.now_iso()))
        _audit(conn, "clarification.sent", "message", message_id, after={"subject": subject})


def gating_reasons(rfx_id: int) -> dict[int, str]:
    """Vendor id -> 'Does not meet question 3: certificate expired 31 Jan 2025 (...)' for
    vendors failing a requirement: why, in plain words, from the pass-rule check's note."""
    questions, answers = questionnaire(rfx_id)
    out: dict[int, str] = {}
    for q in questions:
        if not q["is_gating"]:
            continue
        for (qid, vendor_id), a in answers.items():
            if qid == q["id"] and a["verdict"] == "fail" and vendor_id not in out:
                why = copy.requirement_reason(a["rationale"], q["text"])
                out[vendor_id] = (f"Does not meet question {q['q_no']}: "
                                  f"{why[:1].lower() + why[1:]}")
    return out


ACTION_WORDS = audit.ACTION_WORDS  # the words live in level/agents/analyst/audit.py


def audit_trail(rfx_id: int) -> list[dict]:
    """This event's recorded changes, newest first, in plain words (UX audit B8)."""
    return [{"actor": r["actor"], "When": r["when"], "Who": r["who"], "What": r["what"],
             "Details": r["details"]} for r in reversed(audit.trail(rfx_id))]
