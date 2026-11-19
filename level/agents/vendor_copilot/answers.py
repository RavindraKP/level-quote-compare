"""Writing what a vendor tells us through the reply link.

An answer never edits an extracted value. It is stored as a NEW claim or term whose evidence
is the vendor's own chat message, the flags it answers are closed by "vendor", and the cells
are rebuilt by code (pipeline.run.post_checks). A new claim supersedes the earlier reading
because it is newer (level/core/cells.py: later supersedes earlier per vendor x line).
"""

from __future__ import annotations

import json
import re

import sqlalchemy as sa

from level import db
from level.agents.vendor_copilot.link import LinkScope
from level.agents.vendor_copilot.questions import (
    NOT_OUR_LINE,
    Question,
    line_blockers,
    open_notes,
    open_questions,
    rfx_summary,
)
from level.core import cells as cells_mod
from level.core import landed
from level.pipeline import run as pipeline_run
from level.types import FlagType, Severity

# Fixed answers, in the plain words the prompt asks for (the old tokens are still accepted).
NO_BID = {"not offered", "not offering", "no bid", "no_bid", "not quoting"}
INCLUDED = {"included", "freight included", "delivered"}
CONFIRMED = {"yes", "y", "confirmed", "still applies"}  # the price or reading stands
EVIDENCE_MAX = 600  # characters of the vendor's message kept as the receipt quote
# "one" in "the price is for one piece": small number words a vendor may type.
NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "ten": "10", "twelve": "12", "dozen": "12", "fifty": "50", "hundred": "100",
                "a hundred": "100", "one hundred": "100", "thousand": "1000"}


def is_answer(value: str, words: set[str]) -> bool:
    """True if the answer is one of the fixed words (any case, spaces or underscores)."""
    return value.strip().lower().replace("_", " ").rstrip(".") in {w.replace("_", " ")
                                                                  for w in words}


class AnswerError(ValueError):
    """The answer cannot be recorded as given; the message says what to ask the vendor."""


def _digits(text: str) -> str:
    return re.sub(r"[\s,]", "", text or "")


def check_in_message(value: str, message: str) -> None:
    """No number without a receipt: a number we record must appear in the vendor's words."""
    if not any(ch.isdigit() for ch in value):
        return
    if value.strip() == "1":
        return  # "it is per piece" means one; the words are the receipt
    if _digits(value) not in _digits(message):
        raise AnswerError("Not recorded: that figure is not in the supplier's own words. Ask "
                          "them, in plain words, to type the figure themselves.")


def _find(scope: LinkScope, question_id: str) -> Question:
    for q in open_questions(scope):  # only this vendor's open questions can be answered
        if q.id == question_id:
            return q
    if question_id == "freight":  # a second place's freight after the first answer closed it
        return Question(id="freight", kind="freight", text="")
    raise AnswerError("Not recorded: that question is already settled or does not exist. Use "
                      "only the open questions listed, and do not mention this to the supplier.")


def _claim(conn, claim_id: int | None) -> dict | None:
    if claim_id is None:
        return None
    return conn.execute(sa.select(db.quote_claim).where(db.quote_claim.c.id == claim_id)
                        ).mappings().first()


def _new_claim(conn, scope, q, run_id, evidence, *, price, currency, basis, item_text) -> int:
    claim_id = conn.execute(db.quote_claim.insert().values(
        rfx_id=scope.rfx_id, vendor_id=scope.vendor_id, run_id=run_id, row_index=0,
        vendor_item_text=item_text, raw_price=price, raw_currency=currency,
        raw_uom_text=None, basis=basis, scope="line", **evidence)).inserted_primary_key[0]
    for line_id in q.line_ids:
        conn.execute(db.claim_line.insert().values(
            # The vendor's own price, but not yet confirmed as we convert it (a per-kg price
            # becomes a per-piece price by the line's weight). Confirming the line on the
            # summary marks it vendor-confirmed (UX audit H1).
            claim_id=claim_id, rfx_line_id=line_id, status="vendor_stated",
            match_rationale="stated by the vendor through the reply link"))
    return claim_id


def _places(conn, rfx_id: int) -> list[str]:
    """Every delivery place on the request's lines."""
    return sorted({p for p in conn.execute(sa.select(db.rfx_line.c.plant).where(
        db.rfx_line.c.rfx_id == rfx_id)).scalars() if p})


def _freight_places(conn, rfx_id: int, asked: list[str] | None, value: str,
                    unit: str | None, message: str) -> list[str]:
    """The request's delivery places a freight answer is for; [] means every place.

    The co-pilot may name them (`asked`); each must be a place on the request and appear in
    the vendor's own words. Otherwise code finds them in the vendor's sentence.
    """
    places = _places(conn, rfx_id)
    by_name = {p.lower(): p for p in places}
    chosen = []
    for name in asked or []:
        place = by_name.get(name.strip().lower())
        if place is None or not landed.names_place(message, place):
            raise AnswerError(f"Not recorded: '{name}' is not a delivery place the supplier "
                              f"named. Ask which delivery place this freight is for.")
        chosen.append(place)
    if chosen:
        return chosen
    found = landed.answer_places(value, unit, message, places)
    if found is None:
        raise AnswerError("Not recorded: the message gives freight for more than one place. "
                          "Record each place's figure with its own call, naming the place.")
    return found


def _close(conn, flag_ids: list[int], resolution: str) -> None:
    conn.execute(db.flag.update().where(db.flag.c.id.in_(flag_ids or [-1])).values(
        state="closed", closed_by="vendor", closed_at=db.now_iso(), resolution=resolution))


def _place_claim(conn, scope: LinkScope, q: Question, value: str, message: str) -> None:
    """The vendor says which RFx line(s) a price is for: map the claim to them."""
    if value.strip().lower() == "none":
        conn.execute(db.quote_claim.update().where(db.quote_claim.c.id == q.claim_id)
                     .values(rule_text=NOT_OUR_LINE))
        return
    numbers = [int(n) for n in re.findall(r"\d+", value)]
    if not numbers:
        raise AnswerError("Not recorded: no line number. Ask which line of the request the "
                          "price is for.")
    for n in numbers:
        check_in_message(str(n), message)
    line_ids = dict(conn.execute(sa.select(db.rfx_line.c.line_no, db.rfx_line.c.id).where(
        db.rfx_line.c.rfx_id == scope.rfx_id, db.rfx_line.c.line_no.in_(numbers))).all())
    missing = [n for n in numbers if n not in line_ids]
    if missing:
        raise AnswerError(f"Not recorded: the request has no line {missing[0]}. Ask which "
                          f"line it is for.")
    for n in numbers:
        conn.execute(db.claim_line.insert().values(
            claim_id=q.claim_id, rfx_line_id=line_ids[n], status="proposed",
            match_rationale="the vendor named this line through the reply link"))
    q.line_nos = numbers


NEW_POINT = "new_point"  # something the vendor adds that no open question asked about
# A new point about delivery cost opens a freight question (UX audit H2). Plain words only;
# nothing category-specific.
_FREIGHT_WORDS = re.compile(r"\b(freight|delivery|transport|carriage|cartage)\b", re.I)
_EXTRA_WORDS = re.compile(r"\b(extra|actuals?|additional|charged|not included|excluded)\b",
                          re.I)


def _evidence(vendor_message: str, chat_message_id: int | None) -> dict:
    return {"attachment_id": None, "evidence_ok": True,
            "evidence_quote": vendor_message.strip()[:EVIDENCE_MAX],
            "evidence_locator": f"reply link chat, message {chat_message_id or '?'}"}


def record_new_point(scope: LinkScope, value: str, vendor_message: str,
                     chat_message_id: int | None = None) -> dict:
    """Store a point the vendor raised on their own as a note on their quote. If it says
    delivery costs extra, open a freight question so the delivered price is not assumed."""
    value = (value or "").strip()
    if not value:
        raise AnswerError("Not recorded: no answer given. Ask the question again in plain "
                          "words.")
    # A note, not a price: no number is recorded from it, and the receipt is the message.
    evidence = _evidence(vendor_message, chat_message_id)
    opens_freight = bool(_FREIGHT_WORDS.search(value) and _EXTRA_WORDS.search(value))
    with db.begin() as conn:
        run_id = conn.execute(db.extraction_run.insert().values(
            stage="vendor_answer", status="ok", raw_output=json.dumps(
                {"question_id": NEW_POINT, "value": value}))).inserted_primary_key[0]
        conn.execute(db.vendor_term.insert().values(
            rfx_id=scope.rfx_id, vendor_id=scope.vendor_id, run_id=run_id,
            type="clarification", text=value, is_conditional=False, **evidence))
        if opens_freight:  # carries a run id, so the grid checks keep it
            conn.execute(db.flag.insert().values(
                rfx_id=scope.rfx_id, vendor_id=scope.vendor_id, run_id=run_id,
                target_kind="cell", rfx_line_id=None, type=FlagType.MISSING_INFO.value,
                severity=Severity.WARNING.value,
                reason=f"landed price unknown: the vendor wrote \"{value}\"",
                suggested_action="Ask the vendor for the freight charge.", state="open"))
        conn.execute(db.audit_event.insert().values(
            actor="vendor", action="reply_link.answer", target_kind="question",
            after={"question_id": NEW_POINT, "kind": "text", "value": value, "unit": None,
                   "vendor_id": scope.vendor_id, "rfx_id": scope.rfx_id}))
    pipeline_run.post_checks(scope.rfx_id)
    return {"recorded": "new_point", "lines": [], "value": value, "unit": None,
            "currency": rfx_summary(scope)["currency"],
            "open_questions_left": len(open_questions(scope))}


def record_answer(scope: LinkScope, question_id: str, value: str, unit: str | None,
                  vendor_message: str, chat_message_id: int | None = None,
                  places: list[str] | None = None) -> dict:
    """Store one answer. Returns only this vendor's own facts and the questions left."""
    if question_id == NEW_POINT:
        return record_new_point(scope, value, vendor_message, chat_message_id)
    q = _find(scope, question_id)
    value = (value or "").strip()
    unit = (unit or "").strip() or None
    if not value:
        raise AnswerError("Not recorded: no answer given. Ask the question again in plain "
                          "words.")
    if q.kind == "unit":
        value = NUMBER_WORDS.get(value.lower(), value)  # "one" box -> "1"
    if q.kind != "which_line":
        check_in_message(value, vendor_message)
    currency = rfx_summary(scope)["currency"]
    evidence = _evidence(vendor_message, chat_message_id)
    named: list[str] = []  # freight only: the places the answer is for ([] = every place)
    with db.begin() as conn:
        run_id = conn.execute(db.extraction_run.insert().values(
            stage="vendor_answer", status="ok", raw_output=json.dumps(
                {"question_id": q.id, "kind": q.kind, "value": value, "unit": unit}))
        ).inserted_primary_key[0]
        old = _claim(conn, q.claim_id)
        note = f"answered by the vendor via the reply link: {value} {unit or ''}".strip()
        if q.kind == "which_line":
            _place_claim(conn, scope, q, value, vendor_message)
        elif q.kind == "unit":
            if old is None or old["raw_price"] is None:
                raise AnswerError("Not recorded: there is no price on this line yet. Ask for the "
                              "price and what it is per.")
            if unit is None or not value.replace(".", "", 1).isdigit():
                raise AnswerError("Not recorded: the answer needs a number. Ask, for example, "
                                  "'How many pieces does your price cover?' using the line's "
                                  "unit.")
            _new_claim(conn, scope, q, run_id, evidence, price=old["raw_price"],
                       currency=old["raw_currency"], item_text=old["vendor_item_text"],
                       basis={"per": unit, "multiple": value})
            _close(conn, q.flag_ids, note)
        elif q.kind == "confirm_reading" and is_answer(value, CONFIRMED):
            # The note is settled, but the lines are not confirmed from chat: a "yes" to a
            # broad question is not a tick on each line. The vendor confirms lines with the
            # Confirm buttons on the summary (UX audit H1).
            _close(conn, q.flag_ids, "vendor confirmed the reading")
        elif q.kind == "confirm_price" and is_answer(value, CONFIRMED):
            for line_id in q.line_ids:  # the earlier price stands, confirmed by the vendor
                conn.execute(db.claim_line.update().where(
                    db.claim_line.c.claim_id == q.claim_id,
                    db.claim_line.c.rfx_line_id == line_id).values(status="vendor_confirmed"))
            _close(conn, q.flag_ids, "vendor confirmed the earlier price still applies")
        elif q.kind in ("price", "missing_line") and is_answer(value, NO_BID):
            _new_claim(conn, scope, q, run_id, evidence, price=None, currency=None,
                       basis=None, item_text="not offered (vendor, reply link)")
            _close(conn, q.flag_ids, "not_quoted")
            for line_id in q.line_ids:  # a closed record the cell rebuild reads as "not quoted"
                conn.execute(db.flag.insert().values(
                    rfx_id=scope.rfx_id, vendor_id=scope.vendor_id, run_id=run_id,
                    target_kind="cell", rfx_line_id=line_id, type=FlagType.MISSING_INFO.value,
                    severity=Severity.INFO.value, reason="vendor said they are not offering "
                    "this line", state="closed", closed_by="vendor", closed_at=db.now_iso(),
                    resolution="not_quoted"))
        elif q.kind in ("price", "missing_line", "confirm_price"):
            if not any(ch.isdigit() for ch in value):
                raise AnswerError("Not recorded: a price needs a number. Ask for the price in "
                                  "plain words.")
            if unit is None:
                raise AnswerError("Not recorded: no unit. Ask what the price is per (piece, "
                                  "pack of how many, kg...).")
            _new_claim(conn, scope, q, run_id, evidence, price=value,
                       currency=(old["raw_currency"] if old else None) or currency,
                       basis={"per": unit, "multiple": None},
                       item_text=(old["vendor_item_text"] if old else None) or "price stated "
                       "via the reply link")
            _close(conn, q.flag_ids, note)
        elif q.kind == "requirement":
            # A new answer row; the latest answer to a question is the one judged.
            conn.execute(db.answer.insert().values(
                rfx_id=scope.rfx_id, question_id=int(q.id.removeprefix("req-")),
                vendor_id=scope.vendor_id, run_id=run_id, value=value, **evidence))
        elif q.kind == "freight":
            # A NEW term that names only the places the vendor named; the earlier freight
            # term stays and still covers the other places (QA pass 3 B1, H4). The value and
            # unit are kept as written (audit row below); code builds the term (H1).
            named = _freight_places(conn, scope.rfx_id, places, value, unit, vendor_message)
            text = landed.freight_answer_text(
                "included" if is_answer(value, INCLUDED) else value, unit, currency, named)
            conn.execute(db.vendor_term.insert().values(
                rfx_id=scope.rfx_id, vendor_id=scope.vendor_id, run_id=run_id, type="freight",
                text=text, value=None, is_conditional=False, **evidence))
            _close(conn, q.flag_ids, note)
        else:
            conn.execute(db.vendor_term.insert().values(
                rfx_id=scope.rfx_id, vendor_id=scope.vendor_id, run_id=run_id,
                type="clarification", text=value, is_conditional=False, **evidence))
            _close(conn, q.flag_ids, note)
        conn.execute(db.audit_event.insert().values(
            actor="vendor", action="reply_link.answer", target_kind="question",
            after={"question_id": q.id, "kind": q.kind, "value": value, "unit": unit,
                   "places": named, "vendor_id": scope.vendor_id, "rfx_id": scope.rfx_id}))
    if q.kind == "requirement":  # judged by code against the pass rule, as for any reply
        pipeline_run.apply_answers(scope.rfx_id, scope.vendor_id)
    pipeline_run.post_checks(scope.rfx_id)  # cells and grid checks rebuilt by code
    settle_notes(scope)
    left = open_questions(scope)
    return {"recorded": q.kind, "lines": q.line_nos, "value": value, "unit": unit,
            "places": named, "currency": currency, "open_questions_left": len(left)}


def _mark_confirmed(conn, scope: LinkScope, line_ids: list[int] | None) -> int:
    """Mark the vendor's current reading of these lines (None = every line) as confirmed by
    them. Lines with an open question are skipped. Returns the number of lines marked."""
    blocked = set(line_blockers(scope))
    query = sa.select(db.cell.c.claim_id, db.cell.c.rfx_line_id).where(
        db.cell.c.rfx_id == scope.rfx_id, db.cell.c.vendor_id == scope.vendor_id,
        db.cell.c.claim_id.is_not(None), db.cell.c.state.in_(["confirmed", "inferred"]))
    if line_ids is not None:
        query = query.where(db.cell.c.rfx_line_id.in_(line_ids or [-1]))
    marked = 0
    for claim_id, line_id in conn.execute(query).all():
        if line_id in blocked:
            continue
        marked += conn.execute(db.claim_line.update().where(
            db.claim_line.c.claim_id == claim_id, db.claim_line.c.rfx_line_id == line_id
        ).values(status="vendor_confirmed")).rowcount
    return marked


def settle_notes(scope: LinkScope) -> int:
    """Close each line-less reading note once every line it covers is settled by the vendor
    (confirmed, re-priced or declined through the link). Returns how many closed."""
    blocked = set(line_blockers(scope))  # a line with an open question is not settled
    by_vendor = {r["rfx_line_id"] for r in _vendor_settled(scope)} - blocked
    closed = 0
    with db.begin() as conn:
        for note in open_notes(scope):
            if note["lines"] and set(note["lines"]) <= by_vendor:
                _close(conn, [note["id"]], "vendor confirmed the lines this note covers")
                closed += 1
    if closed:
        cells_mod.rebuild_cells(scope.rfx_id)
    return closed


def _vendor_settled(scope: LinkScope) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sa.select(db.cell.c.rfx_line_id).where(
            db.cell.c.rfx_id == scope.rfx_id, db.cell.c.vendor_id == scope.vendor_id,
            db.cell.c.confirmed_by == "vendor")).mappings()]


def confirm(scope: LinkScope, line_ids: list[int] | None = None) -> int:
    """The vendor confirms 'how we read your quote' for the ticked lines, or for every line
    when line_ids is None. Lines with an open question are left as they are."""
    with db.begin() as conn:
        marked = _mark_confirmed(conn, scope, line_ids)
        conn.execute(db.audit_event.insert().values(
            actor="vendor", action="reply_link.confirm", target_kind="vendor",
            target_id=scope.vendor_id,
            after={"lines_confirmed": marked, "lines_asked": line_ids or "all",
                   "rfx_id": scope.rfx_id}))
    cells_mod.rebuild_cells(scope.rfx_id)
    settle_notes(scope)
    return marked


def last_confirmed(scope: LinkScope) -> str | None:
    """When this vendor last confirmed lines on this event's link, or None."""
    with db.connect() as conn:
        rows = conn.execute(sa.select(db.audit_event.c.at, db.audit_event.c.after).where(
            db.audit_event.c.action == "reply_link.confirm",
            db.audit_event.c.target_id == scope.vendor_id).order_by(db.audit_event.c.id.desc())
        ).all()
    return next((at for at, after in rows
                 if (after or {}).get("rfx_id") == scope.rfx_id
                 and (after or {}).get("lines_confirmed")), None)


def correct(scope: LinkScope, rfx_line_id: int) -> None:
    """The vendor says a line was read wrongly: undo their confirmation and ask again."""
    with db.begin() as conn:
        line = conn.execute(sa.select(db.rfx_line.c.id).where(
            db.rfx_line.c.id == rfx_line_id, db.rfx_line.c.rfx_id == scope.rfx_id)).scalar()
        if line is None:
            raise AnswerError("That line is not in this request.")
        own_claims = sa.select(db.quote_claim.c.id).where(
            db.quote_claim.c.vendor_id == scope.vendor_id)
        conn.execute(db.claim_line.update().where(
            db.claim_line.c.rfx_line_id == rfx_line_id,
            db.claim_line.c.claim_id.in_(own_claims)).values(status="proposed"))
        run_id = conn.execute(db.extraction_run.insert().values(
            stage="vendor_correction", status="ok")).inserted_primary_key[0]
        # Carries a run id, so the grid checks (which redo run-less cell flags) keep it.
        conn.execute(db.flag.insert().values(
            rfx_id=scope.rfx_id, vendor_id=scope.vendor_id, run_id=run_id, target_kind="cell",
            rfx_line_id=rfx_line_id, type=FlagType.MISSING_INFO.value,
            severity=Severity.WARNING.value,
            reason="you told us this line was read wrongly; please give the right price",
            suggested_action="Ask the vendor for the correct price.", state="open"))
        conn.execute(db.audit_event.insert().values(
            actor="vendor", action="reply_link.correct", target_kind="rfx_line",
            target_id=rfx_line_id, after={"vendor_id": scope.vendor_id}))
    cells_mod.rebuild_cells(scope.rfx_id)
