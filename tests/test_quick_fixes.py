"""Pre-deployment fixes: display wording, totals, unread vendors, requirement reasons,
what-if caveats, clarification grouping and the Q15 grading. No model is called."""

from decimal import Decimal

import pytest
from components import data

from evals import analyst as eval_analyst
from level import copy, db
from level.agents.analyst.tools import merge_caveats
from level.pipeline import clarify
from level.types import CellState, FlagType

# --- Item 2 and 3: wording and money on screen ----------------------------------------------


@pytest.mark.parametrize("raw, shown", [
    ("Line 12 is also flagged for a spec deviation",
     "Line 12 also has a point to check for a spec deviation"),
    ("the landed (delivered) price", "The delivered price"),
    ("Uses history.FY25 = 42.12 INR per piece", "Uses last year's price (FY25): ₹42.12 per piece"),
    ("Total INR 47756042.00 a year", "Total ₹47756042 a year"),
    ("USD/INR 90 is a what-if", "USD/INR 90 is a what-if"),
])
def test_plain_wording(raw, shown):
    assert copy.plain(raw) == shown


def test_totals_in_whole_rupees_and_unit_prices_to_two_decimals():
    text = copy.indian_numbers(copy.plain(
        "₹3,30,02,493.5 in total; ₹9,99,271.70 at stake; ₹27.1350 per piece; ₹4.53 Cr; "
        "₹50,756,767.50"))
    assert text == ("₹3,30,02,494 in total; ₹9,99,272 at stake; ₹27.14 per piece; ₹4.53 Cr; "
                    "₹5,07,56,768")


def test_vendor_words_in_quotation_marks_are_left_as_written():
    assert copy.plain('They wrote “INR 4 per piece”') == 'They wrote “INR 4 per piece”'
    assert copy.plain("INR 4 per piece", as_written=True) == "INR 4 per piece"


def test_receipt_steps_in_words_without_padding():
    text, source = copy.conversion_step("per 1000 each -> per 1 each", "divide by 1000",
                                        Decimal("20400.0000"), Decimal("20.4000"), "as quoted")
    assert text == "Per 1,000 pieces → per piece: 20,400.00 → divide by 1,000 → 20.40"
    assert source == "as the vendor quoted"
    assert copy.conversion_step("USD -> INR", "multiply by 86.70", Decimal("0.344"),
                                Decimal("29.8248"), "fx.USDINR")[1] == "USD to INR rate"
    assert "standard unit" not in copy.step_source("standard unit")


# --- Item 5: why a vendor does not meet a requirement ---------------------------------------


def test_requirement_reason_from_the_check_note():
    why = copy.requirement_reason("expires 2025-01-31 (attached document), before 2025-03-14",
                                  "Do you hold a valid quality certificate? Attach it.")
    assert why == ("Certificate expired 31 Jan 2025 (from the attached document); it must be "
                   "valid on 14 Mar 2025")
    assert copy.requirement_reason("answered no; needed yes") == \
        "Answered no; this requirement needs yes"
    assert copy.requirement_reason("12 <= 10 is false") == \
        "Gave 12; this requirement needs 10 or less"


# --- Item 6: what-if caveats -----------------------------------------------------------------


def test_what_if_rate_wins_and_unverified_lines_are_said_once():
    base = ["12 prices in this option not yet verified (assumed, to check or mismatch): "
            "lines 1, 3, 6 and 13.", "USD prices converted at ₹86.70 per USD."]
    what_if = ["USD to INR taken as ₹90.00 for this option, in place of the reference rate "
               "of ₹86.70.", "15 prices in this option not yet verified (assumed, to check or "
               "mismatch): lines 1, 2, 3, 14 and 15."]
    for merged in (merge_caveats(base, what_if), merge_caveats(what_if, base)):
        assert sum("not yet verified" in c for c in merged) == 1
        assert [c for c in merged if c.startswith("USD")] == [what_if[0]]


# --- Item 7: clarification emails ------------------------------------------------------------


def _group(kind, question, snippet="", lines=(), note=False, stake=0):
    return clarify.Group(key=(kind.value, question), kind=kind, snippet=snippet,
                         lines=list(lines), question=question, note=note, stake=Decimal(stake))


def test_freight_asked_once_and_box_unit_asked_once():
    groups = [
        _group(FlagType.MISSING_INFO, "We were not sure how to read this part of your "
               "quotation: freight for one plant is on actuals. Please clarify.", note=True),
        _group(FlagType.MISSING_INFO, 'You wrote "on actuals". Please let us know the freight '
               "charge.", snippet="on actuals", stake=10),
        _group(FlagType.MISSING_INFO, "We have assumed 1 box = 1 piece. Please let us know "
               "what the unit means.", lines=(12, 13, 20), stake=50),
        _group(FlagType.PLAUSIBILITY, 'Your quotation gives these prices "/ box". How many '
               "sets does that price cover?", snippet="/ box", lines=(13,), stake=40),
    ]
    merged = clarify._merge_notes(groups)
    assert len(merged) == 2
    assert sum("freight" in g.question for g in merged) == 1
    unit = next(g for g in merged if g.question.startswith("We have assumed"))
    assert unit.lines == [12, 13, 20]


@pytest.fixture
def partly_quoted(tmp_db):
    with db.begin() as conn:
        rfx = conn.execute(db.rfx.insert().values(title="Annual contract for widgets",
                                                  status="collecting")).inserted_primary_key[0]
        read, unread = (conn.execute(db.vendor.insert().values(name=n)).inserted_primary_key[0]
                        for n in ("North", "South"))
        lines = {}
        for no in (1, 2, 3):
            lines[no] = conn.execute(db.rfx_line.insert().values(
                rfx_id=rfx, line_no=no, description=f"Widget {no}", canonical_uom="piece",
                annual_qty="100")).inserted_primary_key[0]
        for v in (read, unread):
            conn.execute(db.invitation.insert().values(rfx_id=rfx, vendor_id=v,
                                                       status="replied"))
            conn.execute(db.message.insert().values(rfx_id=rfx, vendor_id=v, direction="in",
                                                    channel="email"))
        cid = conn.execute(db.quote_claim.insert().values(
            rfx_id=rfx, vendor_id=read, raw_price="5", raw_currency="INR",
            raw_uom_text="each", evidence_quote="W1 5", evidence_ok=True)).inserted_primary_key[0]
        conn.execute(db.claim_line.insert().values(claim_id=cid, rfx_line_id=lines[1]))
        for v in (read, unread):  # the rebuild stores "not quoted" for read and unread alike
            for no in (1, 2, 3):
                quoted = v == read and no == 1
                conn.execute(db.cell.insert().values(
                    rfx_id=rfx, vendor_id=v, rfx_line_id=lines[no],
                    state="confirmed" if quoted else "not_quoted",
                    claim_id=cid if quoted else None, unit_price_inr="5" if quoted else None,
                    reason="" if quoted else "vendor did not quote this line"))
    return {"rfx": rfx, "read": read, "unread": unread, "lines": lines}


def test_unquoted_lines_are_asked_but_never_for_an_unread_reply(partly_quoted):
    d = clarify.draft(partly_quoted["rfx"], partly_quoted["read"], use_model=False)
    assert d is not None and clarify.UNQUOTED_QUESTION in d.body
    assert "Line 2: Widget 2" in d.body and "Line 3: Widget 3" in d.body
    assert "Line 1:" not in d.body and "Lines:" not in d.body
    assert clarify.draft(partly_quoted["rfx"], partly_quoted["unread"], use_model=False) is None


# --- Item 4: a reply not read yet is not "not quoted" ----------------------------------------


def test_unread_reply_is_not_read_yet_on_compare(partly_quoted):
    grid = data.load_grid(partly_quoted["rfx"])
    for no, line_id in partly_quoted["lines"].items():
        unread = grid.cells[(partly_quoted["unread"], line_id)]
        assert unread.state == CellState.AWAITING and "not read yet" in unread.reason
        read = grid.cells[(partly_quoted["read"], line_id)]
        assert read.state == (CellState.CONFIRMED if no == 1 else CellState.NOT_QUOTED)


# --- Item 8: Q15 --------------------------------------------------------------------------


def test_q15_scores_a_clear_refusal_to_add_different_units():
    want = {"mixed_total": Decimal(2115000), "units": ["piece", "set"]}
    refusal = ("The annual quantity is 20,40,000 pieces across 29 lines, plus 75,000 sets. Sets "
               "are a different unit, so I have kept them separate rather than adding them.")
    assert eval_analyst.refuses_mixed_units(refusal, want)
    assert not eval_analyst.refuses_mixed_units("The total is 21,15,000 units.", want)
    assert not eval_analyst.refuses_mixed_units("20,40,000 pieces and 75,000 sets.", want)
