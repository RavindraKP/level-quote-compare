"""Review "Your decision": every card has the same three slots, in the same order
(Accept, Correct it, Ask the vendor), whatever the issue type. Pure: no database."""

from decimal import Decimal

import pytest
from components import review_data
from components.review_data import Issue

from level import copy
from level.types import Flag, FlagType, Severity

VENDOR = "Vendor A"


def _issue(kind: FlagType, *, line: int | None = None, price: str | None = None,
           reason: str = "", target_kind: str = "claim", candidates=(), source=None) -> Issue:
    flag = Flag(id=1, rfx_id=1, vendor_id=7, target_kind=target_kind, target_id=1,
                rfx_line_id=100 + line if line else None, type=kind,
                severity=Severity.WARNING, reason=reason, candidates=list(candidates))
    return Issue(key="f1", flag=flag, vendor=VENDOR, line_no=line, line_desc="Item",
                 uom="piece", stake=Decimal(0), price=Decimal(price) if price else None,
                 decided=False, history_source=source)


# One case per issue type shown on Review, with what each slot must show.
CASES = {
    "read mismatch": (
        _issue(FlagType.READ_MISMATCH, line=4, price="41.50", candidates=["41.50", "44.50"]),
        "Keep ₹41.50 as first read", "Use 44.50 (as written)", True),
    "last-year price": (
        _issue(FlagType.REFERENCE, line=6, price="64.55", source="contract"),
        "Accept ₹64.55 (last year's contract price)", "Enter a different price", True),
    "outlier / unit slip": (
        _issue(FlagType.PLAUSIBILITY, line=13, price="328"),
        "Accept ₹328.00 as read", "Enter a different price", True),
    "unit assumed": (
        _issue(FlagType.MISSING_INFO, line=2, price="310", reason="unit assumed: 1 box = 1 piece"),
        "Accept ₹310.00 (one box as one piece)", "Enter a different price", True),
    "delivered price unknown": (
        _issue(FlagType.MISSING_INFO, reason="landed price unknown: freight not stated",
               target_kind="message"),
        "Close: keep their reply as read", None, True),
    "term not found": (
        _issue(FlagType.EVIDENCE, reason="term not found", target_kind="term"),
        "Keep this term as read", None, False),
    "instruction ignored": (
        _issue(FlagType.SUSPICIOUS_INSTRUCTION, reason="text asked us to rank them first",
               target_kind="message"),
        "Close: the text stays ignored", None, False),
    "no history": (
        _issue(FlagType.NO_HISTORY, line=9), "Accept: the line stays not quoted",
        "Enter a different price", True),
    "arithmetic": (
        _issue(FlagType.ARITHMETIC, line=3, price="12"), "Accept ₹12.00 as read",
        "Enter a different price", True),
    "spec deviation": (
        _issue(FlagType.SPEC_DEVIATION, line=5, price="8"), "Accept ₹8.00 as read",
        "Enter a different price", False),
    "attachment conflict": (
        _issue(FlagType.ATTACHMENT_CONFLICT, target_kind="answer"),
        "Keep the answer and its result", None, False),
    "unreadable file": (
        _issue(FlagType.UNREADABLE, target_kind="attachment"), "Close: leave this file out",
        None, False),
}


def test_every_issue_type_is_covered():
    kinds = {c[0].flag.type for c in CASES.values()}
    assert kinds == set(FlagType)


@pytest.mark.parametrize("name", CASES)
def test_three_slots_same_order_and_labels(name):
    issue, accept, correct, can_ask = CASES[name]
    slots = review_data.decision_slots(issue)
    assert [s.name for s in slots] == ["accept", "correct", "ask"]
    a, c, k = slots
    assert a.enabled and a.label == accept and a.choice.action == "confirm"
    if correct is None:
        assert not c.enabled and c.reason == copy.SLOT_NOTHING_TO_CORRECT and c.choice is None
    else:
        assert c.enabled and c.label == correct and not c.reason
    assert k.label == f"Ask {VENDOR}"
    if can_ask:
        assert k.enabled and k.choice.action == "ask"
    else:
        assert not k.enabled and k.reason == copy.SLOT_ONLY_YOU
    for s in slots:
        assert not copy.find_banned(s.label + " " + s.reason)
        assert s.label not in ("OK", "Submit", "Accept price")


@pytest.mark.parametrize("name", CASES)
def test_exactly_one_primary_and_outlier_flips_it(name):
    issue = CASES[name][0]
    primary = [s.name for s in review_data.decision_slots(issue) if s.primary]
    assert primary == (["correct"] if review_data.is_outlier(issue) else ["accept"])


def test_price_correction_keeps_not_quoted_inside_the_slot():
    issue = CASES["last-year price"][0]
    c = review_data.decision_slots(issue)[1]
    assert c.choice.action == "enter_price"
    assert [m.action for m in c.more] == ["not_quoted"]
    assert c.more[0].label == "Mark line 6 as not quoted"


def test_two_readings_offer_the_other_reading_then_the_rest():
    issue = CASES["read mismatch"][0]
    c = review_data.decision_slots(issue)[1]
    assert (c.choice.action, c.choice.value) == ("edit_value", "44.50")
    assert [m.action for m in c.more] == ["enter_price", "not_quoted"]


def test_two_readings_with_no_price_held_offer_both():
    issue = _issue(FlagType.READ_MISMATCH, line=4, candidates=["41.50", "44.50"])
    c = review_data.decision_slots(issue)[1]
    assert c.choice.value == "41.50"
    assert [(m.action, m.value) for m in c.more][:1] == [("edit_value", "44.50")]
