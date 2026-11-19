"""What a vendor may see through their reply link. Every function takes a LinkScope and reads
only that RFx and that vendor's own rows (non-negotiable 10).

- rfx_summary: the request itself (lines, units, terms) — what was in their RFx email.
- open_questions: the gaps only they can close, from open flags (via
  pipeline.clarify.questions_for_vendor) plus lines their quote left blank.
- quote_summary: "here is how we read your quote", built by code from their own cells.

Never returned: other vendors, last year's prices, peer medians, scenarios, rankings, or a
cell's internal reason text (a plausibility reason quotes peer prices).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal

import sqlalchemy as sa

from level import copy, db
from level.agents.vendor_copilot.link import LinkScope
from level.pipeline.clarify import questions_for_vendor
from level.types import FlagType

KINDS = ("which_line", "unit", "price", "confirm_price", "missing_line", "freight",
         "confirm_reading", "requirement", "text")
UNCLEAR = "unclear in the source: "  # reason prefix of a reading note with no line of its own
NOT_OUR_LINE = "vendor: not one of the RFx lines"  # rule_text marker on an unplaceable claim

# What record_answer expects as (value, unit) for each kind of question. Fixed answers are
# plain words a supplier would say ("not offered", "included", "yes"), never internal tokens,
# because the model may repeat them to the supplier (UX audit B3).
ANSWER_FORMAT = {
    "which_line": "value = the request line number(s) the supplier names, e.g. '12' or "
                  "'12, 14', or 'none' if it is not one of the request's lines; unit = null",
    "unit": "value = how many of the line's unit the quoted price covers, as the supplier "
            "said it (e.g. '100'); unit = the line's unit",
    "price": "value = the price exactly as the supplier wrote it, or 'not offered' if they "
             "are not offering it; unit = what it is per, in their words (e.g. 'piece', "
             "'100 pcs', 'kg')",
    "confirm_price": "value = 'yes' if the supplier says the price shown still applies, "
                     "otherwise the new price as written; unit = null for yes, else what the "
                     "price is per",
    "missing_line": "value = 'not offered' if they are not offering the line, otherwise the "
                    "price as written; unit = what the price is per",
    "freight": "value = 'included' if delivery to the plant is in the price, otherwise the "
               "freight amount as written; unit = what it is per (e.g. 'piece', 'trip'); "
               "places = the delivery places this figure is for, or null if none is named",
    "confirm_reading": "value = 'yes' if the supplier says the reading is right, otherwise "
                       "their correction in a short sentence; unit = null",
    "requirement": "value = the supplier's answer to the request's question, in their words "
                   "(e.g. 'yes', a number, or a date); unit = null",
    "text": "value = the supplier's answer in a short sentence; unit = null",
}


@dataclass
class Question:
    id: str  # stable while the gap is open: "line-<id>", "claim-<id>", "freight", "flag-<id>"
    kind: str
    text: str
    line_ids: list[int] = field(default_factory=list)
    line_nos: list[int] = field(default_factory=list)
    flag_ids: list[int] = field(default_factory=list)
    claim_id: int | None = None  # the vendor's own claim the answer replaces, if any

    def for_model(self) -> dict:
        """The question as the co-pilot sees it: no internal ids except the question id."""
        return {"question_id": self.id, "kind": self.kind, "lines": self.line_nos,
                "question": self.text, "answer_format": ANSWER_FORMAT[self.kind]}


def _rows(query) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(query).mappings()]


def rfx_summary(scope: LinkScope) -> dict:
    """The request as the vendor received it. No last price, no incumbent."""
    rfx = _rows(sa.select(db.rfx.c.title, db.rfx.c.base_currency, db.rfx.c.terms)
                .where(db.rfx.c.id == scope.rfx_id))[0]
    lines = _rows(sa.select(db.rfx_line.c.line_no, db.rfx_line.c.description,
                            db.rfx_line.c.spec, db.rfx_line.c.canonical_uom,
                            db.rfx_line.c.annual_qty, db.rfx_line.c.plant)
                  .where(db.rfx_line.c.rfx_id == scope.rfx_id).order_by(db.rfx_line.c.line_no))
    vendor = _rows(sa.select(db.vendor.c.name).where(db.vendor.c.id == scope.vendor_id))[0]
    return {"title": rfx["title"], "currency": rfx["base_currency"] or "INR",
            "terms": rfx["terms"] or {}, "vendor": vendor["name"], "lines": lines}


def rfx_document(scope: LinkScope) -> tuple[str, bytes]:
    """(file name, PDF) of the RFx document this vendor was sent: the request only."""
    from level.agents.copilot import rfx_doc  # local: copilot.draft imports this package

    return rfx_doc.filename(scope.rfx_id), rfx_doc.rfx_pdf(scope.rfx_id)


def latest_reply(scope: LinkScope) -> dict | None:
    rows = _rows(sa.select(db.message).where(
        db.message.c.rfx_id == scope.rfx_id, db.message.c.vendor_id == scope.vendor_id,
        db.message.c.direction == "in").order_by(db.message.c.id.desc()).limit(1))
    return rows[0] if rows else None


def _own_cells(scope: LinkScope) -> list[dict]:
    return _rows(sa.select(db.cell, db.rfx_line.c.line_no, db.rfx_line.c.description,
                           db.rfx_line.c.canonical_uom)
                 .join(db.rfx_line, db.rfx_line.c.id == db.cell.c.rfx_line_id)
                 .where(db.cell.c.rfx_id == scope.rfx_id, db.cell.c.vendor_id == scope.vendor_id)
                 .order_by(db.rfx_line.c.line_no))


def _claim_lines(claim_id: int) -> list[int]:
    return [r["rfx_line_id"] for r in _rows(sa.select(db.claim_line.c.rfx_line_id).where(
        db.claim_line.c.claim_id == claim_id, db.claim_line.c.status != "rejected"))]


def _unplaced_claims(scope: LinkScope) -> list[dict]:
    """This vendor's priced claims that matching put on no line (and they did not disown)."""
    mapped = sa.select(db.claim_line.c.claim_id)
    return _rows(sa.select(db.quote_claim).where(
        db.quote_claim.c.rfx_id == scope.rfx_id, db.quote_claim.c.vendor_id == scope.vendor_id,
        db.quote_claim.c.raw_price.is_not(None), db.quote_claim.c.id.not_in(mapped),
        sa.or_(db.quote_claim.c.rule_text.is_(None),
               db.quote_claim.c.rule_text != NOT_OUR_LINE)).order_by(db.quote_claim.c.id))


def _is_unit_gap(flag: dict) -> bool:
    """A price we have but cannot place on the line's unit (per box of how many?)."""
    return (flag["type"] == FlagType.PLAUSIBILITY or flag["reason"].startswith("unit assumed")
            or flag["reason"].startswith("cannot convert"))


def _mentions(text: str, words: str | None) -> bool:
    """True if the vendor's own item words appear, as whole words, in a reading note."""
    words = (words or "").strip()
    if len(words) < 3:
        return False  # too short to tell one item from another
    return re.search(rf"(?<!\w){re.escape(words.lower())}(?!\w)", text.lower()) is not None


def note_lines(flag: dict) -> list[int]:
    """The RFx lines a line-less reading note is about: lines of the claims from the same
    read whose item words the note quotes. Empty if the note names no item."""
    if flag["rfx_line_id"] or not flag["reason"].startswith(UNCLEAR):
        return []
    claims = _rows(sa.select(db.quote_claim.c.id, db.quote_claim.c.vendor_item_text).where(
        db.quote_claim.c.vendor_id == flag["vendor_id"],
        db.quote_claim.c.run_id == flag["run_id"]))
    lines: set[int] = set()
    for c in claims:
        if _mentions(flag["reason"], c["vendor_item_text"]):
            lines.update(_claim_lines(c["id"]))
    return sorted(lines)


def open_notes(scope: LinkScope) -> list[dict]:
    """Open line-less reading notes on this vendor's quote, each with the lines it covers."""
    notes = _rows(sa.select(db.flag).where(
        db.flag.c.rfx_id == scope.rfx_id, db.flag.c.vendor_id == scope.vendor_id,
        db.flag.c.state == "open", db.flag.c.rfx_line_id.is_(None),
        db.flag.c.reason.startswith(UNCLEAR)))
    return [{**f, "lines": note_lines(f)} for f in notes]


def open_questions(scope: LinkScope) -> list[Question]:
    """Every gap only this vendor can close, one question per gap, in line order."""
    reply = latest_reply(scope)
    if reply is None:
        return []  # nothing received yet, so nothing to ask
    asked = questions_for_vendor(reply["id"])  # already vendor-safe wording
    flags = {f["id"]: f for f in _rows(sa.select(db.flag).where(
        db.flag.c.id.in_([q["flag_id"] for q in asked] or [-1]),
        db.flag.c.vendor_id == scope.vendor_id))}
    cells = {c["rfx_line_id"]: c for c in _own_cells(scope)}
    line_no = {lid: c["line_no"] for lid, c in cells.items()}

    groups: dict[str, Question] = {}
    texts: dict[str, list[str]] = {}
    types: dict[str, set[str]] = {}  # flag types per question
    notes: list[tuple[dict, list[int], str]] = []  # line-less notes that name lines
    for q in asked:
        f = flags.get(q["flag_id"])
        if f is None:
            continue
        if related := note_lines(f):
            notes.append((f, related, q["question"]))
            continue
        lines = [f["rfx_line_id"]] if f["rfx_line_id"] else (
            _claim_lines(f["target_id"]) if f["target_kind"] == "claim" else [])
        if len(lines) == 1:
            key, kind = f"line-{lines[0]}", "unit" if _is_unit_gap(f) else "price"
        elif lines:
            key, kind = f"claim-{f['target_id']}", "price"
        elif f["reason"].startswith("landed price unknown"):
            key, kind = "freight", "freight"
        else:
            key, kind = f"flag-{f['id']}", "text"
        group = groups.setdefault(key, Question(id=key, kind=kind, text="", line_ids=lines))
        if kind == "unit":
            group.kind = "unit"  # a unit gap on a line outranks a price question on it
        group.flag_ids.append(f["id"])
        types.setdefault(key, set()).add(f["type"])
        if f["target_kind"] == "claim":
            group.claim_id = f["target_id"]
        texts.setdefault(key, [])
        if q["question"] not in texts[key]:
            texts[key].append(q["question"])

    for key, group in groups.items():
        if group.claim_id is None and len(group.line_ids) == 1:
            group.claim_id = cells.get(group.line_ids[0], {}).get("claim_id")
        cell = cells.get(group.line_ids[0]) if len(group.line_ids) == 1 else None
        quoted = f" You quoted: {cell['as_quoted']}." if cell and cell["as_quoted"] else ""
        unit = f" (the line is priced per {cell['canonical_uom']})" if cell else ""
        group.text = " ".join(texts[key]) + quoted + unit
        own = cell and cell["unit_price_inr"]
        if types.get(key) == {FlagType.REFERENCE.value} and own:
            # QA M17: their own earlier price is taken; ask them to confirm it, not re-quote.
            group.kind = "confirm_price"
            group.text = (f"Your reply refers to your earlier price. We have taken your own "
                          f"previous price for this line: "
                          f"{copy.unit_price(Decimal(str(cell['unit_price_inr'])))} per "
                          f"{cell['canonical_uom']}. Does it still apply? If not, please give "
                          f"the new price.")
        group.line_nos = sorted(line_no[lid] for lid in group.line_ids if lid in line_no)

    _fold_read_notes(groups, flags)
    covered = {lid for g in groups.values() for lid in g.line_ids}
    for f, related, text in notes:
        # A note whose lines all have their own question is not asked again: it closes once
        # those lines are settled (answers.settle_notes). Otherwise it asks the vendor to
        # confirm the reading of the lines no other question covers.
        settled = {lid for lid, c in cells.items() if c["confirmed_by"] == "vendor"}
        rest = [lid for lid in related if lid not in covered and lid not in settled]
        if rest:
            groups[f"note-{f['id']}"] = Question(
                id=f"note-{f['id']}", kind="confirm_reading", text=text, line_ids=rest,
                line_nos=sorted(line_no[lid] for lid in rest if lid in line_no),
                flag_ids=[f["id"]])

    for c in _unplaced_claims(scope):  # prices we read but could not put on any line
        groups[f"claim-{c['id']}"] = Question(
            id=f"claim-{c['id']}", kind="which_line", claim_id=c["id"],
            text=f"You quoted \"{c['vendor_item_text'] or c['evidence_quote']}\" at "
                 f"{c['raw_price']} {c['raw_uom_text'] or ''}. Which line of the request is "
                 f"that for?")
    currency = rfx_summary(scope)["currency"]
    covered = {lid for g in groups.values() for lid in g.line_ids}
    for lid, c in cells.items():  # lines the quote left blank: no claim at all
        if c["state"] == "not_quoted" and c["claim_id"] is None and lid not in covered:
            groups[f"line-{lid}"] = Question(
                id=f"line-{lid}", kind="missing_line", line_ids=[lid], line_nos=[c["line_no"]],
                text=f"Line {c['line_no']} ({c['description']}) has no price in your quote. "
                     f"Are you not offering it, or was it missed? If missed, what is your price "
                     f"in {currency} and per what unit (the line is priced per "
                     f"{c['canonical_uom']})?")
    for q in open_requirements(scope):  # required answers we could not judge (UX audit M12)
        groups[q.id] = q
    order = {"which_line": -1, "unit": 0, "price": 0, "confirm_price": 0, "missing_line": 1,
             "freight": 2, "requirement": 2, "confirm_reading": 3, "text": 3}
    return sorted(groups.values(),
                  key=lambda g: (order[g.kind], g.line_nos[0] if g.line_nos else 10**6, g.id))


def _fold_read_notes(groups: dict[str, Question], flags: dict[int, dict]) -> None:
    """A price we could not read is asked once. A line-less note from the same read that
    quotes one of the unreadable figures (e.g. '4?.50') is the same point, so it joins that
    price question and closes with it, instead of being asked again (UX audit H2)."""
    for group in list(groups.values()):
        reads = [flags[i] for i in group.flag_ids
                 if flags[i]["type"] == FlagType.READ_MISMATCH.value]
        figures = {c for f in reads for c in (f["candidates"] or []) if len(str(c)) >= 3}
        runs = {f["run_id"] for f in reads}
        for key, other in list(groups.items()):
            note = (flags.get(other.flag_ids[0]) if other.kind == "text" and key in groups
                    else None)
            if note and note["run_id"] in runs and any(str(c) in note["reason"]
                                                         for c in figures):
                group.flag_ids += other.flag_ids
                del groups[key]


def open_requirements(scope: LinkScope) -> list[Question]:
    """Required questions of the request whose answer from this vendor is missing or could
    not be judged yes or no. Only the question and the vendor's own answer are shown."""
    with db.connect() as conn:
        required = conn.execute(sa.select(db.question).where(
            db.question.c.rfx_id == scope.rfx_id, db.question.c.is_gating.is_(True))
            .order_by(db.question.c.q_no)).mappings().all()
        rows = conn.execute(sa.select(db.answer).where(
            db.answer.c.rfx_id == scope.rfx_id, db.answer.c.vendor_id == scope.vendor_id)
            .order_by(db.answer.c.id)).mappings().all()
    latest = {a["question_id"]: a for a in rows}  # a later answer replaces an earlier one
    out = []
    for q in required:
        a = latest.get(q["id"])
        if a is not None and a["verdict"] != "unclear":
            continue
        said = (f" We could not tell your answer from your reply: \"{a['value']}\"."
                if a is not None and a["value"] else " We did not find an answer in your reply.")
        out.append(Question(id=f"req-{q['id']}", kind="requirement",
                            text=f"Question {q['q_no']} of the request asks: \"{q['text']}\""
                                 f"{said} What is your answer?"))
    return out


def blocks_confirm(q: Question) -> bool:
    """Every question must be answered before confirming, except "is our reading right?"
    (confirming those lines is the answer) and the questionnaire, which is not a price."""
    return q.kind not in ("confirm_reading", "requirement")


def line_blockers(scope: LinkScope, qs: list[Question] | None = None) -> dict[int, str]:
    """rfx_line_id -> 'question N' for lines that cannot be confirmed yet. N is the number
    the page shows next to the question."""
    qs = open_questions(scope) if qs is None else qs
    out: dict[int, str] = {}
    for n, q in enumerate(qs, start=1):
        if blocks_confirm(q):
            for lid in q.line_ids:
                out.setdefault(lid, f"question {n}")
    return out


def quote_summary(scope: LinkScope) -> dict:
    """'Here is how we read your quote', in the buyer's units, from this vendor's cells."""
    rfx = rfx_summary(scope)
    blockers = line_blockers(scope)
    rows = []
    for c in _own_cells(scope):
        if c["state"] == "awaiting":
            continue
        if c["rfx_line_id"] in blockers:
            status = "question open"
        elif c["state"] == "not_quoted":
            status = "not offered" if c["confirmed_by"] == "vendor" else "no price"
        elif c["unit_price_inr"] is None:
            status = "could not read"
        else:
            # "confirmed by you" only when the buyer's grid shows it as confirmed too
            status = ("confirmed by you" if c["confirmed_by"] == "vendor"
                      and c["state"] == "confirmed" else "read")
        rows.append({"line_no": c["line_no"], "rfx_line_id": c["rfx_line_id"],
                     "description": c["description"], "as_quoted": c["as_quoted"],
                     "price_per_unit": c["unit_price_inr"], "unit": c["canonical_uom"],
                     "status": status, "blocked_by": blockers.get(c["rfx_line_id"]),
                     "can_confirm": status == "read"})
    # Every freight term, oldest first: a later answer for one place adds to the earlier
    # term, it does not replace it (QA pass 3 H4).
    freight = _rows(sa.select(db.vendor_term.c.text, db.vendor_term.c.value).where(
        db.vendor_term.c.rfx_id == scope.rfx_id, db.vendor_term.c.vendor_id == scope.vendor_id,
        db.vendor_term.c.type == "freight").order_by(db.vendor_term.c.id))
    # Answers are stored as "freight INR 2 per piece, to X": drop our own lower-case word.
    said = [re.sub(r"^freight:?\s+", "", (f["text"] or f["value"] or "").strip()).rstrip(".;")
            for f in freight]
    return {"currency": rfx["currency"], "lines": rows,
            "freight": "; ".join(s for s in said if s) or "not stated",
            "gaps": [r["line_no"] for r in rows if r["status"] in ("no price", "question open")]}


def as_dicts(questions: list[Question]) -> list[dict]:
    return [asdict(q) for q in questions]
