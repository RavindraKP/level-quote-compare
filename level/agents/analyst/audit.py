"""The audit trail in plain words, for one event (Award page, award memo, receipts).

The audit table holds every event's entries, with a target (a flag, a message, a line...)
but no event id. An entry belongs to this event when its target does (UX audit B8):
- flag, message, scenario, rfx_line: the target row's own rfx_id;
- rfx: the event itself;
- a vendor's answer on the reply link: the flag, line or price its question is about;
- a vendor's confirmation: that vendor's reply-link conversation on this event;
- an assumption change: every event (assumptions are global inputs).

Each entry is turned into "what happened" and "details" in the buyer's words, never the
stored JSON (UX audit B1, B6, B8). Nothing here computes a price.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa

from level import copy, db
from level.types import FlagType

WHO = {"buyer": "You", "vendor": "Vendor", "system": "Checks", "copilot": "Co-pilot"}

ACTION_WORDS = {
    "flag.confirm": "Accepted", "flag.edit_value": "Entered a different price",
    "flag.not_quoted": "Marked not quoted", "flag.apply_fix": "Used the suggested value",
    "flag.undo": "Undid a decision",
    "clarification.sent": "Sent an email to a vendor", "reply.added": "Added a reply",
    "pipeline.processed": "Read a reply", "rfx.awarded": "Awarded",
    "assumption.set": "Changed an assumption",
    "reply_link.answer": "Vendor answered a question",
    "reply_link.confirm": "Vendor confirmed their prices",
    "reply_link.correct": "Vendor reported a wrong reading",
    "rfx.created": "Started the RFx", "rfx.header": "Changed the RFx title",
    "rfx.terms": "Changed the RFx terms", "rfx.sent": "Sent the RFx",
    "rfx.import": "Copied a past event", "rfx.lines.upsert": "Added or changed lines",
    "rfx.lines.remove": "Removed lines", "rfx.questions.upsert": "Added or changed questions",
    "rfx.questions.remove": "Removed questions",
}
# Decisions on several issues at once share a batch number and are shown as one row.
BATCH_WORDS = {"flag.confirm": "Accepted {n} prices", "flag.not_quoted":
               "Marked {n} lines not quoted", "flag.undo": "Undid {n} decisions"}


def _rows(conn, stmt) -> list[dict]:
    return [dict(r) for r in conn.execute(stmt).mappings()]


def _context(conn, rfx_id: int) -> dict:
    """Everything needed to place and describe this event's entries, read once."""
    lines = {r["id"]: r for r in _rows(conn, sa.select(
        db.rfx_line.c.id, db.rfx_line.c.line_no, db.rfx_line.c.canonical_uom,
        db.rfx_line.c.plant).where(
        db.rfx_line.c.rfx_id == rfx_id))}
    return {
        "rfx_id": rfx_id,
        "lines": lines,
        "flags": {r["id"]: r for r in _rows(conn, sa.select(
            db.flag.c.id, db.flag.c.vendor_id, db.flag.c.rfx_line_id, db.flag.c.type,
            db.flag.c.target_kind, db.flag.c.target_id).where(db.flag.c.rfx_id == rfx_id))},
        "messages": {r["id"]: r for r in _rows(conn, sa.select(
            db.message.c.id, db.message.c.vendor_id, db.message.c.subject).where(
            db.message.c.rfx_id == rfx_id))},
        "scenarios": {r["id"]: r for r in _rows(conn, sa.select(
            db.scenario.c.id, db.scenario.c.name).where(db.scenario.c.rfx_id == rfx_id))},
        "claims": {r["id"]: r for r in _rows(conn, sa.select(
            db.quote_claim.c.id, db.quote_claim.c.vendor_id).where(
            db.quote_claim.c.rfx_id == rfx_id))},
        "link_vendors": set(conn.execute(sa.select(db.thread.c.vendor_id).where(
            db.thread.c.rfx_id == rfx_id, db.thread.c.kind == "vendor_link")).scalars()),
        "vendors": dict(conn.execute(sa.select(db.vendor.c.id, db.vendor.c.name)).all()),
    }


def _question_target(after: dict) -> tuple[str, int] | None:
    """A reply-link question id ('flag-6', 'note-6', 'line-12', 'claim-3') -> (kind, id)."""
    m = re.fullmatch(r"(flag|note|line|claim)-(\d+)", str(after.get("question_id") or ""))
    if not m:
        return None
    return ("flag" if m[1] in ("flag", "note") else m[1]), int(m[2])


def belongs(e: dict, ctx: dict) -> bool:
    """Is this audit entry about this event?"""
    kind, tid, after = e["target_kind"], e["target_id"], e["after"] or {}
    if kind == "rfx":
        return tid == ctx["rfx_id"]
    if kind in ("flag", "message", "scenario", "rfx_line"):
        table = {"flag": "flags", "message": "messages", "scenario": "scenarios",
                 "rfx_line": "lines"}[kind]
        return tid in ctx[table]
    if kind == "question" and isinstance(after, dict):
        if after.get("rfx_id") == ctx["rfx_id"]:  # freight, questionnaire, new points (P3-H3)
            return True
        target = _question_target(after)
        if target is None:
            return False
        table = {"flag": "flags", "line": "lines", "claim": "claims"}[target[0]]
        return target[1] in ctx[table]
    if kind == "vendor":
        return tid in ctx["link_vendors"]
    return kind == "assumption"


# --- Wording -------------------------------------------------------------------------------


def _money(value) -> str:
    """'₹3.28' for a number; the text in quotes otherwise. Never raises (UX audit B1)."""
    try:
        return copy.unit_price(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return f"“{value}”"


def _line_words(nos: list[int]) -> str:
    nos = sorted(set(nos))
    if not nos:
        return ""
    if len(nos) == 1:
        return f"line {nos[0]}"
    return f"lines {', '.join(map(str, nos[:-1]))} and {nos[-1]}"


def _flag_subject(flag: dict | None, ctx: dict) -> str:
    """'Mahalaxmi Packaging, line 6: price taken from last year'."""
    if flag is None:
        return ""
    vendor = ctx["vendors"].get(flag["vendor_id"], "")
    line = ctx["lines"].get(flag["rfx_line_id"])
    where = f"line {line['line_no']}" if line else "their reply"
    title = copy.ISSUE_TITLE.get(FlagType(flag["type"]), "") if flag["type"] else ""
    return f"{vendor}, {where}" + (f": {title[:1].lower() + title[1:]}" if title else "")


def _decision_details(e: dict, ctx: dict) -> str:
    after = e["after"] if isinstance(e["after"], dict) else {}
    bits = [_flag_subject(ctx["flags"].get(e["target_id"]), ctx)]
    if e["action"] == "flag.edit_value" and after.get("value") not in (None, ""):
        bits.append(f"price set to {_money(after['value'])}")
    if after.get("note"):
        bits.append(f"note: {after['note']}")
    return "; ".join(b for b in bits if b)


def _batch_details(entries: list[dict], ctx: dict) -> str:
    flags = [ctx["flags"].get(e["target_id"]) for e in entries]
    vendors = sorted({ctx["vendors"].get(f["vendor_id"], "") for f in flags if f})
    nos = [ctx["lines"][f["rfx_line_id"]]["line_no"] for f in flags
           if f and f["rfx_line_id"] in ctx["lines"]]
    return ", ".join(v for v in vendors if v) + (f": {_line_words(nos)}" if nos else "")


def _answer_details(after: dict, ctx: dict) -> str:
    """'Bharat Corrugators, line 13: “Box holds 100 sets”'."""
    target = _question_target(after)
    nos = []
    if target and target[0] == "flag" and target[1] in ctx["flags"]:
        line = ctx["lines"].get(ctx["flags"][target[1]]["rfx_line_id"])
        nos = [line["line_no"]] if line else []
    elif target and target[0] == "line" and target[1] in ctx["lines"]:
        nos = [ctx["lines"][target[1]]["line_no"]]
    vendor = ctx["vendors"].get(after.get("vendor_id"), "")
    where = f", {_line_words(nos)}" if nos else ""
    value, unit = str(after.get("value") or ""), after.get("unit")
    said = " ".join(x for x in (value, unit or "") if x)
    said = copy.plain(said, capitalise=False, as_written=True)  # their words, as written
    if after.get("kind") == "freight":  # QA pass 3 H3: freight answers are shown too
        places = after.get("places") or []
        to = f" to {' and '.join(places)}" if places else ""
        return f"{vendor}: freight{to} “{said}”"
    if after.get("kind") == "requirement":
        return f"{vendor}: answer to a request question “{said}”"
    if after.get("kind") == "text" and after.get("question_id") == "new_point":
        return f"{vendor}: added a note “{said}”"
    if after.get("kind") == "confirm_price" and value.upper() == "YES":
        return f"{vendor}{where}: the price still applies"
    if re.fullmatch(r"\d+(\.\d+)?", value):  # a price they gave: '₹62.00 per piece'
        return f"{vendor}{where}: {_money(value)}" + (f" per {unit}" if unit else "")
    shown = " ".join(x for x in (value, unit or "") if x)
    return f"{vendor}{where}: “{copy.plain(shown, capitalise=False, as_written=True)}”"


def _list(value) -> str:
    """A stored list or value in words: [31] -> '31', ['a', 'b'] -> 'a, b'."""
    if isinstance(value, list | tuple):
        return ", ".join(copy.field_label(str(v)) if isinstance(v, str) else str(v)
                         for v in value)
    return str(value)


def details(e: dict, ctx: dict) -> str:
    """What one entry recorded, in words."""
    action, after = e["action"], e["after"] if isinstance(e["after"], dict) else {}
    vendors = ctx["vendors"]
    if action.startswith("flag."):
        return _decision_details(e, ctx)
    if action == "clarification.sent":
        msg = ctx["messages"].get(after.get("message_id") or e["target_id"]) or {}
        who = vendors.get(msg.get("vendor_id"), "")
        return f"To {who}: {after.get('subject') or msg.get('subject') or ''}".strip(": ")
    if action in ("reply.added", "pipeline.processed"):
        msg = ctx["messages"].get(e["target_id"]) or {}
        who = vendors.get(msg.get("vendor_id"), "a vendor")
        if action == "reply.added":
            files = after.get("files") or []
            return f"From {who}" + (f": {', '.join(files)}" if files else ", in the email")
        return (f"{copy.possessive(who)} reply: "
                f"{copy.plural(int(after.get('claims') or 0), 'price')} found, "
                f"{copy.plural(int(after.get('flags') or 0), 'point')} raised")
    if action == "rfx.awarded":
        n = after.get("unconfirmed_prices")
        name = copy.plain(after.get("scenario") or "")
        return name + (f"; {copy.plural(int(n), 'price')} not yet verified" if n else "")
    if action == "assumption.set":
        key = after.get("key", "")
        value = after.get("value")
        shown = copy.pct(Decimal(str(value)) * 100) if key in ("gst_rate", "cost_of_capital") \
            and value is not None else value
        return f"{copy.assumption_label(key)} set to {shown}"
    if action == "reply_link.answer":
        return _answer_details(after, ctx)
    if action == "reply_link.confirm":
        n = after.get("lines_confirmed")
        return (f"{vendors.get(e['target_id'], '')} confirmed "
                f"{copy.plural(int(n or 0), 'price')} as read")
    if action == "reply_link.correct":
        line = ctx["lines"].get(e["target_id"])
        who = vendors.get(after.get("vendor_id"), "The vendor")
        return f"{who} said line {line['line_no'] if line else '?'} was read wrongly"
    if action in ("rfx.created", "rfx.header"):
        return after.get("title") or ""
    if action == "rfx.terms":
        return ", ".join(copy.field_label(k) for k in after if k not in ("ref", "title"))
    if action == "rfx.sent":
        return f"To {copy.plural(int(after.get('vendors') or 0), 'vendor')}"
    if action == "rfx.import":
        return (f"{copy.plural(int(after.get('lines') or 0), 'line')} and "
                f"{copy.plural(int(after.get('questions') or 0), 'question')} copied")
    if action.startswith("rfx.lines."):
        return _line_words([int(n) for n in after.get("line_nos") or []]).capitalize()
    if action.startswith("rfx.questions."):
        nos = after.get("q_nos") or []
        return ("Question " if len(nos) == 1 else "Questions ") + _list(nos) if nos else ""
    # Anything else: its fields in words, ids left out.
    return "; ".join(f"{copy.field_label(k)}: {_list(v)}" for k, v in after.items()
                     if v not in (None, "", [], {}) and not k.endswith("id"))


def what(e: dict) -> str:
    return ACTION_WORDS.get(e["action"]) or copy.plain(
        e["action"].split(".")[-1].replace("_", " "))


def who(e: dict, ctx: dict) -> str:
    """'You', 'Co-pilot', 'Checks', or the vendor's name for a vendor's own action."""
    after = e["after"] if isinstance(e["after"], dict) else {}
    if e["actor"] == "vendor":
        vid = after.get("vendor_id") or (e["target_id"] if e["target_kind"] == "vendor" else None)
        return ctx["vendors"].get(vid, "Vendor")
    return WHO.get(e["actor"], copy.plain(e["actor"]))


def _row(e: dict, ctx: dict, batch: list[dict] | None = None) -> dict:
    if batch and len(batch) > 1 and e["action"] in BATCH_WORDS:
        what_text, detail = BATCH_WORDS[e["action"]].format(n=len(batch)), \
            _batch_details(batch, ctx)
    else:
        what_text, detail = what(e), details(e, ctx)
    return {"actor": e["actor"], "at": e["at"], "when": copy.date_time(e["at"]),
            "who": who(e, ctx), "what": what_text, "details": detail}


def _entries(conn, ctx: dict) -> list[dict]:
    rows = _rows(conn, sa.select(db.audit_event).where(
        db.audit_event.c.target_kind != "job").order_by(db.audit_event.c.id))
    return [e for e in rows if belongs(e, ctx)]


def _rows_for(entries: list[dict], ctx: dict) -> list[dict]:
    """Entries -> display rows, oldest first; a batch decision becomes one row."""
    out, done = [], set()
    for e in entries:
        if e["id"] in done:
            continue
        batch_no = (e["after"] or {}).get("batch") if isinstance(e["after"], dict) else None
        batch = [x for x in entries if batch_no and isinstance(x["after"], dict)
                 and x["after"].get("batch") == batch_no and x["action"] == e["action"]]
        done.update(x["id"] for x in batch)
        out.append(_row(e, ctx, batch))
    return out


def trail(rfx_id: int) -> list[dict]:
    """This event's audit trail, oldest first: {"actor", "at", "when", "who", "what",
    "details"}, every value in plain words."""
    with db.connect() as conn:
        ctx = _context(conn, rfx_id)
        return _rows_for(_entries(conn, ctx), ctx)


def for_line(rfx_id: int, vendor_id: int, rfx_line_id: int) -> list[dict]:
    """The history of one price (a receipt): decisions on its issues, the vendor's answers
    and corrections for that line, their confirmations, and assumption changes."""
    with db.connect() as conn:
        ctx = _context(conn, rfx_id)
        mine = {fid for fid, f in ctx["flags"].items() if f["vendor_id"] == vendor_id
                and f["rfx_line_id"] == rfx_line_id}

        def about_this_line(e: dict) -> bool:
            kind, after = e["target_kind"], e["after"] if isinstance(e["after"], dict) else {}
            if kind == "flag":
                return e["target_id"] in mine
            if kind == "question" and after.get("kind") == "freight":
                # A freight answer changes this line's delivered price if it names no place
                # or names this line's place (QA pass 3 H3).
                place = (ctx["lines"].get(rfx_line_id) or {}).get("plant")
                places = after.get("places") or []
                return after.get("vendor_id") == vendor_id and (not places or place in places)
            if kind == "question":
                target = _question_target(after)
                return after.get("vendor_id") == vendor_id and target is not None and (
                    (target[0] == "flag" and target[1] in mine)
                    or (target[0] == "line" and target[1] == rfx_line_id))
            if kind == "rfx_line":
                return e["target_id"] == rfx_line_id and after.get("vendor_id") == vendor_id
            if kind == "vendor":
                return e["target_id"] == vendor_id
            return kind == "assumption"

        return _rows_for([e for e in _entries(conn, ctx) if about_this_line(e)], ctx)
