"""A small fake of the system under test, used only to prove the scoring works.

`write_fake_results` fills the cell, flag, term and answer tables from ground truth, then
applies deliberate mistakes; the tests check that the scorecard catches exactly those.
Nothing here is ever used to produce a real scorecard (runs made with it are labelled FAKE).
"""

from __future__ import annotations

from decimal import Decimal

import sqlalchemy as sa

from evals import analyst
from evals.truth import cells, dec
from level import db

# Each mistake: (kind, vendor key, which line). The tests assert the scorecard finds them.
MISTAKES = [
    ("silent_wrong_price", "A", "first"),   # wrong price shown as confirmed -> silent error
    ("flagged_wrong_price", "D", "partition"),  # wrong price but needs review -> not silent
    ("drop_flag", "B", "arithmetic"),       # missed grand-total mismatch -> X07 fails
]


def _line_for(truth: dict, which: str) -> int:
    if which == "partition":
        return next(ln for ln in truth["lines"] if ln["kind"] == "partition")["line_no"]
    return truth["lines"][0]["line_no"]


def write_fake_results(truth: dict, ids: dict, mistakes=MISTAKES) -> None:
    wrong = {(k, _line_for(truth, w)): kind for kind, k, w in mistakes if "price" in kind}
    dropped = {(k, t) for kind, k, t in mistakes if kind == "drop_flag"}
    rfx_id = ids["rfx_id"]
    with db.begin() as conn:
        for key, vendor in truth["vendors"].items():
            vid = ids["vendors"][key]
            for tc in vendor["cells"]:
                unit, state = dec(tc["unit_price_inr"]), tc["expected_state"]
                kind = wrong.get((key, tc["line_no"]))
                if kind == "silent_wrong_price":
                    unit, state = unit * Decimal("1.1"), "confirmed"
                elif kind == "flagged_wrong_price":
                    unit, state = unit * 100, "needs_review"
                usd = "USD" in (tc["as_quoted"] or "")
                conn.execute(db.cell.insert().values(
                    rfx_id=rfx_id, vendor_id=vid, rfx_line_id=ids["lines"][tc["line_no"]],
                    state=state, reason="fake", as_quoted=tc["as_quoted"],
                    unit_price_inr=None if unit is None else str(unit),
                    landed_price_inr=tc["landed_price_inr"],
                    conversion_steps=[{"label": "fake step"}] if unit is not None else [],
                    assumptions={"fx.USDINR": "86.70"} if usd else {}))
            _terms(conn, truth, key, vid, rfx_id)
            _answers(conn, truth, key, vid, rfx_id, ids)
        for key, type_, nos in _expected_flags(truth):
            if (key, type_) in dropped:
                continue
            for no in nos or [None]:
                conn.execute(db.flag.insert().values(
                    rfx_id=rfx_id, vendor_id=ids["vendors"][key], target_kind="cell",
                    rfx_line_id=None if no is None else ids["lines"][no], type=type_,
                    severity="warning", reason="fake", state="open"))


def _terms(conn, truth, key, vid, rfx_id):
    vendor = truth["vendors"][key]
    terms = [("freight", vendor.get("freight"), False)]
    if vendor.get("payment_days"):
        terms.append(("payment", f"{vendor['payment_days']} days", False))
    if vendor.get("validity_days"):
        terms.append(("validity", "USD/INR clause 3%" if key == "C" else "valid", False))
    if key == "B":
        terms += [("discount", "2%", True), ("tooling", "Rs 4,500 per colour", False)]
    for type_, text, conditional in terms:
        conn.execute(db.vendor_term.insert().values(
            rfx_id=rfx_id, vendor_id=vid, type=type_, text=text, value=text,
            is_conditional=conditional, evidence_quote=text))


def _answers(conn, truth, key, vid, rfx_id, ids):
    qids = {r.q_no: r.id for r in conn.execute(sa.select(db.question.c.id, db.question.c.q_no))}
    for q_no, verdict in truth["vendors"][key]["gating"]["by_question"].items():
        conn.execute(db.answer.insert().values(rfx_id=rfx_id, question_id=qids[int(q_no)],
                                               vendor_id=vid, verdict=verdict))


def _expected_flags(truth: dict) -> list[tuple[str, str, list[int]]]:
    """The flags a perfect system would raise on this dataset (eval-side knowledge only)."""
    d, e = cells(truth, "D"), cells(truth, "E")
    flags = [("B", "arithmetic", []), ("D", "attachment_conflict", []),
             ("D", "spec_deviation", [truth["vendors"]["D"]["doc"]["light_line"]]),
             ("D", "plausibility", [n for n, c in d.items() if "/ box" in (c["as_quoted"] or "")
                                    and c["expected_state"] == "needs_review"]),
             ("D", "read_mismatch", [n for n, c in d.items() if "smudged" in c["note"]])]
    unresolved = [n for n, c in e.items() if c["as_quoted"] == "same as last year"
                  and not c["quoted"]]
    if unresolved:  # a reference to a line the vendor has no record for
        flags.append(("E", "no_history", unresolved))
    if "F" in truth["vendors"]:
        flags.append(("F", "suspicious_instruction", []))
    return flags


def fake_ask(truth: dict, wrong_ids=("Q04",)) -> callable:
    """An 'analyst' that states the expected answer, except for the ids in wrong_ids."""
    by_text = {q["text"]: q for q in analyst.questions(truth)}

    def ask(text: str) -> str:
        q = by_text[text]
        want = q["expected"]
        if q["kind"] == "decline":
            return "The data I have does not contain this, so I can't answer it."
        if q["kind"] == "refuse_mixed_units":
            return ("These lines are in different units (" + " and ".join(want["units"]) +
                    "), so I have kept them separate rather than adding them.")
        if q["id"] in wrong_ids:
            return f"About {Decimal(want) * Decimal('1.1'):,.0f} INR."
        if isinstance(want, list):
            return "That would be " + ", ".join(want) + "."
        return f"The answer is {want}."

    return ask


def fake_grader(question: str, answer: str) -> bool:
    return "does not contain" in answer


def fake_process(truth: dict):
    """Stands in for the pipeline on the injection reply: flags it and reads two claims."""
    def process(message_id: int) -> None:
        with db.begin() as conn:
            msg = conn.execute(sa.select(db.message).where(db.message.c.id == message_id)).one()
            for _ in range(2):
                conn.execute(db.quote_claim.insert().values(
                    rfx_id=msg.rfx_id, vendor_id=msg.vendor_id, vendor_item_text="fake",
                    evidence_quote="fake"))
            conn.execute(db.flag.insert().values(
                rfx_id=msg.rfx_id, vendor_id=msg.vendor_id, target_kind="message",
                target_id=message_id, type="suspicious_instruction", severity="critical",
                reason="fake"))
    return process
