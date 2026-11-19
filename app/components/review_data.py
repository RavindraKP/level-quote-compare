"""Reads for the Review page: the issue list, price context, evidence, same-cause groups.

No Streamlit here, so it is testable. Nothing here writes: decisions go through
data.resolve_flag / data.resolve_flags / data.undo_last, the existing audit path.
Every figure is read from SQLite or computed in level/core/review.py, never by the model.

Price context (other vendors' prices, the median, last year's contract price) is for the
buyer only. Nothing in this module is called by the vendor reply link or by email drafting
(non-negotiable 10); tests/test_review_page.py checks that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

from components import data
from level import copy, db
from level.core import review as core
from level.pipeline import clarify
from level.types import Evidence, Flag, FlagType

# Point types that are about one vendor's price on one line (they get the price context).
NOT_PRICE = {FlagType.ATTACHMENT_CONFLICT, FlagType.SUSPICIOUS_INSTRUCTION, FlagType.UNREADABLE}
# Types whose "accept" may be done for many lines at once when the cause is the same.
BULK_TYPES = {FlagType.REFERENCE, FlagType.MISSING_INFO, FlagType.PLAUSIBILITY}


@dataclass
class Issue:
    """One point on the Review page, with everything its row and card need."""

    key: str
    flag: Flag
    vendor: str
    line_no: int | None
    line_desc: str | None
    uom: str | None
    stake: Decimal
    price: Decimal | None  # this vendor's price for the line as the grid holds it now
    decided: bool
    decision: dict = field(default_factory=dict)  # {"action", "value", "note"} once decided
    title: str = ""
    summary: str = ""
    found: str = ""
    history_source: str | None = None  # "contract" or "quote" for a price from last year
    period: str | None = None
    rule_text: str | None = None
    cause: tuple = ()
    why: str = ""  # "Why it matters", in one line
    folded: list[Flag] = field(default_factory=list)  # same line, same root: decided with it

    @property
    def is_price(self) -> bool:
        return self.flag.rfx_line_id is not None and self.flag.type not in NOT_PRICE

    @property
    def vendor_can_answer(self) -> bool:
        return self.flag.type in clarify.VENDOR_CLOSABLE and self.flag.vendor_id is not None


def issue_key(row: dict) -> str:
    """A key that survives a rebuild. Grid-level points (no run id) and questionnaire
    conflicts are deleted and raised again on every rebuild with new ids, so they are keyed
    by what they are about; every other point keeps its id."""
    if row["run_id"] is None and row["target_kind"] == "cell":
        return f"c{row['vendor_id']}-{row['rfx_line_id']}-{row['type']}"
    if row["type"] == FlagType.ATTACHMENT_CONFLICT.value:
        return f"a{row['target_id']}"
    return f"f{row['id']}"


def _decision(text: str | None) -> dict:
    import json

    try:
        out = json.loads(text) if text and text.startswith("{") else {}
    except ValueError:
        out = {}
    return out if isinstance(out, dict) else {}


def issues(rfx_id: int) -> list[Issue]:
    """Open points plus those a person decided, in queue order (core.order_queue)."""
    rows = data._rows(sa.select(db.flag).where(
        db.flag.c.rfx_id == rfx_id,
        sa.or_(db.flag.c.state == "open", db.flag.c.closed_by.in_(("buyer", "vendor")))))
    lines = {ln["id"]: ln for ln in data.lines(rfx_id)}
    names = {v["id"]: v["name"] for v in data.all_vendors()}
    cells = {(c["vendor_id"], c["rfx_line_id"]): c
             for c in data._rows(sa.select(db.cell).where(db.cell.c.rfx_id == rfx_id))}
    claims = {c["id"]: c for c in data._rows(sa.select(
        db.quote_claim.c.id, db.quote_claim.c.rule_text).where(
        db.quote_claim.c.rfx_id == rfx_id))}
    history = _history(rfx_id)
    peers = _peer_prices(rfx_id)
    terms = {t["id"]: t for t in data._rows(sa.select(
        db.vendor_term.c.id, db.vendor_term.c.text, db.vendor_term.c.evidence_quote).where(
        db.vendor_term.c.rfx_id == rfx_id))}
    files_read = _vendors_with_files(rfx_id)
    out = []
    for r in rows:
        f = data._flag_from_row(r)
        line = lines.get(r["rfx_line_id"]) or {}
        cell = cells.get((r["vendor_id"], r["rfx_line_id"])) or {}
        price = data._dec(cell.get("unit_price_inr"))
        qty = data._dec(line.get("annual_qty"))
        vendor = names.get(r["vendor_id"], "Unknown vendor")
        ctx = core.price_context(r["vendor_id"], price, history.get(r["rfx_line_id"], []),
                                 peers.get(r["rfx_line_id"], [])) if line else None
        own = ctx.own_record if ctx else None
        rule = claims.get(r["target_id"], {}).get("rule_text") if r["target_kind"] == "claim" \
            else None
        src = own.source if own and f.type == FlagType.REFERENCE else None
        args = {"vs_median": ctx.vs_median if ctx else None,
                "median": ctx.peer_median if ctx else None}
        found = copy.issue_found(f.type, f.reason, vendor, line.get("line_no"), price,
                                 line.get("canonical_uom"), rule, src,
                                 own.period if own else None, **args)
        why = copy.ISSUE_WHY[f.type]
        if f.type == FlagType.EVIDENCE and f.target_kind == "term":  # M10: say what was checked
            term = terms.get(r["target_id"]) or {}
            words = term.get("evidence_quote") or term.get("text") or ""
            found = copy.term_not_found(vendor, words)
            why = copy.TERM_WHY
        elif copy.is_covering_email_note(f.reason) and r["vendor_id"] in files_read:
            found = copy.covering_email_found(vendor, f.reason)  # H7: their files were read
            why = copy.COVERING_EMAIL_WHY
        out.append(Issue(
            key=issue_key(r), flag=f, vendor=vendor, line_no=line.get("line_no"),
            line_desc=line.get("description"), uom=line.get("canonical_uom"),
            stake=core.review_stake(qty, price, args["vs_median"], args["median"]),
            price=price, decided=r["state"] != "open",
            decision=_decision(r["resolution"]),
            title=copy.issue_title(f.type, f.reason, f.target_kind),
            summary=copy.issue_summary(f.type, f.reason, line.get("line_no"), price,
                                       f.candidates, src, target_kind=f.target_kind, **args),
            found=found, why=why, history_source=src, period=own.period if own else None,
            rule_text=rule,
            cause=(f.type.value, re.sub(r"\d+(\.\d+)?", "#", f.reason) if f.type !=
                   FlagType.MISSING_INFO else f.reason),
        ))
    out = _fold_same_root(out)
    items = [core.QueueItem(i.key, i.flag.vendor_id, i.stake,
                            data.SEVERITY_ORDER[i.flag.severity], i.flag.id or 0) for i in out]
    by_key = {i.key: i for i in out}
    return [by_key[it.key] for it in core.order_queue(items)]


def _vendors_with_files(rfx_id: int) -> set[int]:
    """Vendors with at least one price read from an attached file on this event."""
    return {r["vendor_id"] for r in data._rows(sa.select(db.quote_claim.c.vendor_id).where(
        db.quote_claim.c.rfx_id == rfx_id, db.quote_claim.c.attachment_id.is_not(None)))}


def _fold_same_root(items: list[Issue]) -> list[Issue]:
    """One cause, one point (UX audit M9): a price far from the other quotes and the unit we
    assumed for that same price are the same question ("is this per box of 100?"). The
    unit point is folded into the price check point and decided with it."""
    outliers = {(i.flag.vendor_id, i.flag.rfx_line_id): i for i in items
                if i.flag.type == FlagType.PLAUSIBILITY and i.flag.rfx_line_id
                and i.decided is False}
    kept = []
    for i in items:
        host = outliers.get((i.flag.vendor_id, i.flag.rfx_line_id))
        if host is not None and i is not host and not i.decided and \
                i.flag.type == FlagType.MISSING_INFO and copy.unit_assumed(i.flag.reason):
            host.folded.append(i.flag)
            host.found += " " + copy.folded_note(i.flag.reason)
            continue
        kept.append(i)
    return kept


def is_outlier(issue: Issue) -> bool:
    """A price far from the others: entering a price or asking is the likelier decision."""
    return issue.flag.type == FlagType.PLAUSIBILITY


def is_open(flag_id: int) -> bool:
    """Is the issue still open (not decided in another window meanwhile)?"""
    rows = data._rows(sa.select(db.flag.c.state).where(db.flag.c.id == flag_id))
    return bool(rows) and rows[0]["state"] == "open"


def vendor_headings(items: list[Issue]) -> dict[int | None, dict]:
    """Vendor id -> {"name", "open", "stake"} over the open points given."""
    open_items = [i for i in items if not i.decided]
    totals = core.vendor_totals([core.QueueItem(i.key, i.flag.vendor_id, i.stake, 0, 0)
                                 for i in open_items])
    out: dict[int | None, dict] = {}
    for i in items:
        v = out.setdefault(i.flag.vendor_id, {"name": i.vendor, "open": 0,
                                               "stake": totals.get(i.flag.vendor_id, 0)})
        v["open"] += 0 if i.decided else 1
    return out


def same_cause(items: list[Issue], issue: Issue) -> list[Issue]:
    """Open points for this vendor that share this point's cause and can be accepted together
    (same issue type and the same accept), in line order. [] if fewer than two."""
    if issue.decided or issue.flag.type not in BULK_TYPES or not issue.is_price:
        return []
    candidates = [core.BulkCandidate(i.key, i.flag.id, i.flag.vendor_id, i.cause, i.line_no,
                                     i.price)
                  for i in items if not i.decided and i.is_price and i.flag.type in BULK_TYPES]
    group = core.same_cause_groups(candidates).get((issue.flag.vendor_id, issue.cause), [])
    by_key = {i.key: i for i in items}
    return [by_key[c.key] for c in group]


# --- Price context (buyer only) -------------------------------------------------------------


def _record_dates() -> dict[tuple[str, str], str]:
    """(period, source) -> the date of that record: a past event's award date for contract
    prices, its reply-by date for quotes. Read from the past event's own terms."""
    out = {}
    for r in data._rows(sa.select(db.rfx.c.terms)):
        terms = r["terms"] or {}
        period = terms.get("period")
        if period:
            if terms.get("award_date"):
                out[(period, "contract")] = terms["award_date"]
            if terms.get("due"):
                out[(period, "quote")] = terms["due"]
    return out


def _history(rfx_id: int) -> dict[int, list[core.Record]]:
    """Line id -> every vendor's records for that line last year, oldest first."""
    line_ids = [ln["id"] for ln in data.lines(rfx_id)]
    dates = _record_dates()
    out: dict[int, list[core.Record]] = {}
    for r in data._rows(sa.select(db.price_history).where(
            db.price_history.c.rfx_line_id.in_(line_ids or [-1])).order_by(db.price_history.c.id)):
        price = data._dec(r["unit_price"])
        if price is None:
            continue
        out.setdefault(r["rfx_line_id"], []).append(core.Record(
            vendor_id=r["vendor_id"], price=price, source=r["source"] or "quote",
            period=r["period"] or "", uom=r["uom"],
            date=dates.get((r["period"] or "", r["source"] or ""))))
    return out


def _peer_prices(rfx_id: int) -> dict[int, list[core.PeerPrice]]:
    """Line id -> every vendor's comparable price this year (cells with a price)."""
    out: dict[int, list[core.PeerPrice]] = {}
    for c in data._rows(sa.select(db.cell).where(db.cell.c.rfx_id == rfx_id)):
        price = data._dec(c["unit_price_inr"])
        if price is not None:
            out.setdefault(c["rfx_line_id"], []).append(
                core.PeerPrice(c["vendor_id"], price, c["state"]))
    return out


def price_context(rfx_id: int, issue: Issue) -> dict | None:
    """What the buyer needs to judge one price, as display rows. None for a point that is
    not about one line's price. Buyer only: never passed to anything a vendor sees."""
    if not issue.is_price:
        return None
    line_id = issue.flag.rfx_line_id
    ctx = core.price_context(issue.flag.vendor_id, issue.price,
                             _history(rfx_id).get(line_id, []),
                             _peer_prices(rfx_id).get(line_id, []))
    names = {v["id"]: v["name"] for v in data.all_vendors()}
    own = ctx.own_record
    last = ctx.last_contract
    return {
        "context": ctx,
        "own": copy.record_text(own.price, own.uom or issue.uom, own.source, own.period,
                                own.date) if own else None,
        "peers": [{"Vendor": names.get(p.vendor_id, "Unknown vendor"),
                   f"Price per {issue.uom}": copy.unit_price(p.price),
                   "Status": copy.STATE_LABEL.get(p.state, p.state)}
                  for p in ctx.peers],
        "median": copy.unit_price(ctx.peer_median) if ctx.peer_median is not None else None,
        "vs_median": ctx.vs_median,
        "last_contract": (f"{copy.unit_price(last.price)} per {last.uom or issue.uom} · "
                          f"{names.get(last.vendor_id, 'Unknown vendor')} · {last.period}"
                          + (f", awarded {copy.date(last.date)}" if last.date else ""))
        if last else None,
    }


# --- Evidence -------------------------------------------------------------------------------


@dataclass
class Source:
    evidence: Evidence | None  # the vendor's words and where they are
    file_exists: bool  # a stored file the viewer can open
    email_body: str | None  # the email text, when the words came from an email body


def source(issue: Issue) -> Source:
    """The vendor's words behind a point, and whether there is a file or an email to show."""
    f = issue.flag
    ev = data.flag_evidence(f)
    if ev is None and f.target_kind == "cell" and f.rfx_line_id:
        cell = data._rows(sa.select(db.cell.c.claim_id).where(
            db.cell.c.vendor_id == f.vendor_id, db.cell.c.rfx_line_id == f.rfx_line_id))
        q = data.claim(cell[0]["claim_id"]) if cell else None
        ev = q.evidence if q else None
    att = data.attachment(ev.attachment_id) if ev else None
    exists = bool(att and att["path"] and Path(att["path"]).exists())
    body = None
    if not att:
        body = _email_body(f)
    return Source(evidence=ev if ev and (ev.quote or exists) else None, file_exists=exists,
                  email_body=body)


def _email_body(f: Flag) -> str | None:
    """The email text behind a point read from an email body (no attachment), if any."""
    if f.target_kind == "message":
        rows = data._rows(sa.select(db.flag.c.run_id).where(db.flag.c.id == f.id))
        run_id = rows[0]["run_id"] if rows else None
    else:
        claim_id = f.target_id if f.target_kind == "claim" else None
        if claim_id is None and f.rfx_line_id:
            cell = data._rows(sa.select(db.cell.c.claim_id).where(
                db.cell.c.vendor_id == f.vendor_id, db.cell.c.rfx_line_id == f.rfx_line_id))
            claim_id = cell[0]["claim_id"] if cell else None
        q = data.claim(claim_id)
        run_id = q.run_id if q else None
    return run_email_body(run_id)


def run_email_body(run_id: int | None) -> str | None:
    """The email text a reading run read, when it read an email body (not a file)."""
    if run_id is None:
        return None
    run = data._rows(sa.select(db.extraction_run.c.message_id, db.extraction_run.c.attachment_id)
                     .where(db.extraction_run.c.id == run_id))
    if not run or run[0]["attachment_id"] or not run[0]["message_id"]:
        return None
    msg = data._rows(sa.select(db.message.c.body).where(db.message.c.id == run[0]["message_id"]))
    return (msg[0]["body"] or None) if msg else None


# --- Asking the vendor ----------------------------------------------------------------------


def question_number(rfx_id: int, vendor_id: int, flag_id: int) -> int | None:
    """Which question of the vendor's email carries this point (1-based), from the same
    grouping the email uses. None if it is held back for the next round or not askable."""
    d = clarify.draft(rfx_id, vendor_id, use_model=False)
    if d is None:
        return None
    for n, g in enumerate(d.groups, start=1):
        if flag_id in g.flag_ids:
            return n
    return None
