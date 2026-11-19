"""QA pass 3 B1, H1, H4: a freight answer on the reply link applies only to the places it
names, keeps the vendor's other freight terms, and stores the unit without doubling "per".

Invented wording only: three delivery places (Eastfield, Westbrook, Northgate) on the small
safety-supplies RFx from pipeline_helpers. Line 1 goes to Eastfield, line 3 to Westbrook,
line 4 to Northgate. Freight is per piece; each line's unit is one piece ("each").
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from level import db
from level.agents.vendor_copilot import agent, answers, link, questions
from level.core import landed
from level.pipeline import run as pipeline_run
from tests import pipeline_helpers as H

PLACES = ["Eastfield", "Northgate", "Westbrook"]


# --- Pure rules in level/core/landed.py ---------------------------------------------------


def test_answer_text_keeps_the_unit_once():
    """H1: "Rs 1.50" + "per piece" must not become "INR Rs 1.50 per per piece"."""
    assert landed.freight_answer_text("Rs 1.50", "per piece", "INR") == "freight Rs 1.50 per piece"
    assert landed.freight_answer_text("1.50", "piece", "INR") == "freight INR 1.50 per piece"
    assert landed.freight_answer_text("2 per piece", "piece", "INR") == "freight INR 2 per piece"
    assert landed.freight_answer_text("Rs. 3", "/pc", "INR") == "freight Rs 3 per pc"
    f = landed.parse_freight(landed.freight_answer_text("Rs 1.50", "per per piece", "INR"))
    assert (f.kind, f.amount, f.unit, f.currency) == ("per_unit", Decimal("1.50"), "each", "INR")


def test_answer_text_names_its_places_last():
    text = landed.freight_answer_text("4", "piece", "INR", ["Westbrook"])
    assert text == "freight INR 4 per piece, to Westbrook"
    assert landed.freight_answer_text("extra at actuals", None, "INR", ["Westbrook"]) == (
        "freight extra at actuals, to Westbrook")


def test_places_come_from_the_sentence_holding_the_figure():
    msg = "Thanks for the call. Freight to Westbrook is INR 4 per piece. Rest as quoted."
    assert landed.answer_places("4", "piece", msg, PLACES) == ["Westbrook"]
    assert landed.answer_places("INR 4", "per piece", "Freight is INR 4 per piece to all "
                                "sites.", PLACES) == []
    assert landed.answer_places("extra at actuals (for Northgate)", None, "x", PLACES) == [
        "Northgate"]
    # "4" is not the 4 inside "14": the Eastfield sentence does not hold this figure.
    msg = "Eastfield is 14 per piece; Westbrook is 4 per piece"
    assert landed.answer_places("4", "piece", msg, PLACES) == ["Westbrook"]


def test_two_figures_for_two_places_in_one_sentence_is_not_guessed():
    msg = "Freight to Westbrook is 4 per piece and to Northgate 7 per piece."
    assert landed.answer_places("4", "piece", msg, PLACES) is None


def test_newest_term_covering_the_place_wins():
    terms = [("freight INR 4 per piece, to Westbrook", None),  # newest first
             ("Freight to Eastfield INR 3 per piece; Westbrook at actuals.", None)]
    assert landed.freight_from_terms(terms, "Westbrook", PLACES).amount == Decimal("4")
    assert landed.freight_from_terms(terms, "Eastfield", PLACES).amount == Decimal("3")
    north = landed.freight_from_terms(terms, "Northgate", PLACES)
    assert north.kind == "unknown"  # no term covers it: never a borrowed figure
    assert landed.freight_from_terms([], "Eastfield", PLACES) is None


# --- Through the reply link ----------------------------------------------------------------


def _claim(conn, rfx, vendor, line, price, quote):
    cid = conn.execute(db.quote_claim.insert().values(
        rfx_id=rfx, vendor_id=vendor, vendor_item_text=quote, raw_price=price,
        raw_currency="INR", basis={"per": "each", "multiple": None}, scope="line",
        evidence_locator="email, line 1", evidence_quote=quote, evidence_ok=True,
    )).inserted_primary_key[0]
    conn.execute(db.claim_line.insert().values(claim_id=cid, rfx_line_id=line))


def _event(freight_text: str):
    ids = H.seed()
    rfx, lines, north = ids["rfx"], ids["lines"], ids["vendors"]["North"]
    with db.begin() as conn:
        for no, place in ((1, "Eastfield"), (2, "Eastfield"), (3, "Westbrook"),
                          (4, "Northgate")):
            conn.execute(db.rfx_line.update().where(db.rfx_line.c.id == lines[no]).values(
                plant=place))
        conn.execute(db.invitation.update().where(db.invitation.c.vendor_id == north)
                     .values(status="replied"))
        conn.execute(db.message.insert().values(
            rfx_id=rfx, vendor_id=north, direction="in", channel="email", body="quote",
            received_at="2026-01-01T00:00:00.000000Z"))
        _claim(conn, rfx, north, lines[1], "100", "Helmet 100 each")
        _claim(conn, rfx, north, lines[3], "50", "Vest 50 each")
        _claim(conn, rfx, north, lines[4], "1", "Ties 1 each")
        conn.execute(db.vendor_term.insert().values(
            rfx_id=rfx, vendor_id=north, type="freight", text=freight_text,
            is_conditional=False, evidence_ok=True, evidence_quote=freight_text))
    pipeline_run.post_checks(rfx)
    return SimpleNamespace(rfx=rfx, lines=lines, scope=link.LinkScope(rfx, north))


@pytest.fixture
def setup(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LEVEL_LINK_SECRET", "test-secret")
    return _event


def _cell(ev, line_no):
    with db.connect() as conn:
        return conn.execute(sa.select(db.cell).where(
            db.cell.c.vendor_id == ev.scope.vendor_id,
            db.cell.c.rfx_line_id == ev.lines[line_no])).mappings().one()


def _landed(ev, line_no):
    value = _cell(ev, line_no)["landed_price_inr"]
    return None if value is None else Decimal(str(value))


def _answer(ev, value, unit, message, places=None):
    return agent.run_tool(ev.scope, "record_answer", {
        "question_id": "freight", "value": value, "unit": unit, "places": places},
        message, None)


def test_one_place_answer_changes_only_that_place(setup):
    ev = setup("Freight to Eastfield and Northgate INR 3 per piece; Westbrook at actuals.")
    assert "freight" in {q.id for q in questions.open_questions(ev.scope)}
    assert _landed(ev, 3) is None
    out = _answer(ev, "INR 4", "per piece", "Freight to Westbrook is INR 4 per piece.")
    assert out["places"] == ["Westbrook"]
    assert agent._what(out) == ": freight INR 4 per piece to Westbrook"  # what the vendor sees
    assert _landed(ev, 1) == Decimal("103.00")  # Eastfield keeps its quoted 3
    assert _landed(ev, 3) == Decimal("54.00")   # Westbrook takes the answer
    assert _landed(ev, 4) == Decimal("4.00")    # Northgate keeps its quoted 3


def test_answer_with_places_named_by_the_copilot(setup):
    ev = setup("Freight to Eastfield and Northgate INR 3 per piece; Westbrook at actuals.")
    _answer(ev, "4", "piece", "For the Westbrook site it is 4 rupees a piece.",
            places=["westbrook"])
    assert (_landed(ev, 1), _landed(ev, 3)) == (Decimal("103.00"), Decimal("54.00"))


def test_a_place_the_vendor_did_not_name_is_refused(setup):
    ev = setup("Freight to Eastfield INR 3 per piece; Westbrook at actuals.")
    with pytest.raises(answers.AnswerError):
        _answer(ev, "4", "piece", "Freight is 4 per piece.", places=["Westbrook"])


def test_all_places_answer_applies_everywhere(setup):
    ev = setup("Freight to Eastfield INR 3 per piece; Westbrook at actuals.")
    _answer(ev, "Rs 2", "per piece", "Freight is Rs 2 per piece to every site.")
    assert (_landed(ev, 1), _landed(ev, 3), _landed(ev, 4)) == (
        Decimal("102.00"), Decimal("52.00"), Decimal("3.00"))


def test_two_answers_for_two_places(setup):
    ev = setup("Freight to Eastfield INR 3 per piece.")
    _answer(ev, "4", "piece", "Westbrook: 4 per piece. Northgate: 7 per piece.",
            places=["Westbrook"])
    _answer(ev, "7", "piece", "Westbrook: 4 per piece. Northgate: 7 per piece.",
            places=["Northgate"])
    assert (_landed(ev, 1), _landed(ev, 3), _landed(ev, 4)) == (
        Decimal("103.00"), Decimal("54.00"), Decimal("8.00"))


def test_two_places_in_one_sentence_without_names_is_not_recorded(setup):
    ev = setup("Freight to Eastfield INR 3 per piece.")
    with pytest.raises(answers.AnswerError):
        _answer(ev, "4", "piece", "Westbrook is 4 per piece and Northgate 7 per piece.")
    assert _landed(ev, 3) is None


def test_free_delivery_term_is_kept(setup):
    """H4: an answer for one place does not delete the vendor's other freight terms."""
    ev = setup("Delivery free to Eastfield and Northgate.")
    _answer(ev, "extra at actuals (for Westbrook)", None,
            "For Westbrook freight is extra at actuals.")
    assert _landed(ev, 1) == Decimal("100.00")  # still free
    assert _landed(ev, 4) == Decimal("1.00")
    assert _landed(ev, 3) is None  # at actuals: unknown, not zero
    with db.connect() as conn:
        texts = conn.execute(sa.select(db.vendor_term.c.text).where(
            db.vendor_term.c.type == "freight")).scalars().all()
    assert "Delivery free to Eastfield and Northgate." in texts
    shown = questions.quote_summary(ev.scope)["freight"]
    assert "Delivery free to Eastfield and Northgate" in shown
    assert "extra at actuals" in shown and "freight:" not in shown


def test_unit_is_stored_as_written_and_read_once(setup):
    """H1: the vendor's unit is kept as written; the term reads 'per piece' once."""
    ev = setup("Freight extra.")
    _answer(ev, "Rs 1.50", "per piece", "Freight is Rs 1.50 per piece to all sites.")
    with db.connect() as conn:
        text = conn.execute(sa.select(db.vendor_term.c.text).where(
            db.vendor_term.c.type == "freight").order_by(db.vendor_term.c.id.desc())
        ).scalars().first()
        audit = conn.execute(sa.select(db.audit_event.c.after).where(
            db.audit_event.c.action == "reply_link.answer")).scalars().first()
    assert text == "freight Rs 1.50 per piece"
    assert audit["unit"] == "per piece" and audit["value"] == "Rs 1.50"
    assert _landed(ev, 1) == Decimal("101.50")


def test_verified_line_at_an_uncovered_place_never_borrows_freight(setup):
    """The worst case in B1: a vendor tick must not show another place's freight as
    Verified."""
    ev = setup("Freight to Eastfield INR 3 per piece.")
    _answer(ev, "INR 9", "per piece", "Freight to Northgate is INR 9 per piece.")
    answers.confirm(ev.scope, [ev.lines[1], ev.lines[3]])
    one, three = _cell(ev, 1), _cell(ev, 3)
    assert one["state"] == "confirmed" and one["confirmed_by"] == "vendor"
    assert Decimal(str(one["landed_price_inr"])) == Decimal("103.00")  # not 109
    assert three["landed_price_inr"] is None  # Westbrook: no term covers it
    assert _landed(ev, 4) == Decimal("10.00")


# --- QA pass 3 H2: only amounts and quantities are regrouped ------------------------------


def test_codes_and_file_names_keep_their_digits():
    from level import copy
    for text in ("award-option-20261118-192043.xlsx", "PO 4500012345", "call 9876543210",
                 "pin 411001", "dated 20250222", "ref A1234567"):
        assert copy.indian_numbers(text) == text
    assert copy.indian_numbers("₹50,756,767 and 290000 pieces, total 1234567") == (
        "₹5,07,56,767 and 2,90,000 pieces, total 12,34,567")
    assert copy.indian_numbers("Rs 330024935.50 in all") == "Rs 33,00,24,935.50 in all"


def test_freight_answers_reach_the_audit_trail_and_the_right_lines(setup):
    """QA pass 3 H3: a freight answer is shown to the buyer, on the lines it affects."""
    from level.agents.analyst import audit
    ev = setup("Freight to Eastfield INR 3 per piece.")
    _answer(ev, "INR 4", "per piece", "Freight to Westbrook is INR 4 per piece.")
    text = " ".join(str(r) for r in audit.trail(ev.rfx))
    assert "freight to Westbrook" in text and "INR 4" in text
    westbrook = " ".join(str(r) for r in audit.for_line(ev.rfx, ev.scope.vendor_id, ev.lines[3]))
    eastfield = " ".join(str(r) for r in audit.for_line(ev.rfx, ev.scope.vendor_id, ev.lines[1]))
    assert "freight to Westbrook" in westbrook and "freight" not in eastfield.lower()
