"""WP12: clarification emails built in the six steps of docs/UX_GUIDE.md section 1.

Invented event: one vendor quotes 13 lines "per bundle" (one unit question), refers to
"rates as per our last order" on three lines, leaves freight open, and has three prices that
were hard to read. No model is called: wording is either fixed or a stubbed model reply.
"""

import re
from types import SimpleNamespace

import pytest

from level import copy, db, llm
from level.pipeline import clarify


def _claim(conn, rfx, vendor, line, price, per, quote, rule=None):
    cid = conn.execute(db.quote_claim.insert().values(
        rfx_id=rfx, vendor_id=vendor, vendor_item_text=quote, raw_price=price,
        raw_currency="INR", raw_uom_text=per, scope="reference" if rule else "line",
        rule_text=rule, evidence_quote=quote, evidence_ok=True)).inserted_primary_key[0]
    conn.execute(db.claim_line.insert().values(claim_id=cid, rfx_line_id=line))
    return cid


def _flag(conn, rfx, vendor, type_, reason, line=None, claim=None, suggested=None, cands=()):
    conn.execute(db.flag.insert().values(
        rfx_id=rfx, vendor_id=vendor, rfx_line_id=line, type=type_, severity="warning",
        reason=reason, suggested_action=suggested, candidates=list(cands), state="open",
        target_kind="claim" if claim else "cell", target_id=claim))


@pytest.fixture
def event(tmp_db):
    with db.begin() as conn:
        rfx = conn.execute(db.rfx.insert().values(
            title="Annual contract for widgets", status="collecting",
            terms={"buyer": "Example Industries Ltd"})).inserted_primary_key[0]
        vendor = conn.execute(db.vendor.insert().values(
            name="Acme Widgets", email="sales@acme.example")).inserted_primary_key[0]
        other = conn.execute(db.vendor.insert().values(name="Zenith Supplies")
                             ).inserted_primary_key[0]
        conn.execute(db.message.insert().values(
            rfx_id=rfx, vendor_id=vendor, direction="in", channel="email",
            received_at="2026-11-10T09:00:00Z"))
        line = {}
        for no in range(1, 21):
            line[no] = conn.execute(db.rfx_line.insert().values(
                rfx_id=rfx, line_no=no, description=f"Widget type {no}", canonical_uom="piece",
                annual_qty="1000")).inserted_primary_key[0]
        for no in range(1, 14):  # 13 lines, one unit question; line 13 has the most at stake
            cid = _claim(conn, rfx, vendor, line[no], "310", "per bundle", f"W{no} 310/bundle")
            conn.execute(db.cell.insert().values(
                rfx_id=rfx, vendor_id=vendor, rfx_line_id=line[no], state="needs_review",
                claim_id=cid, unit_price_inr="310" if no != 13 else "9000"))
            _flag(conn, rfx, vendor, "plausibility", "far above peer median 4.10 (Zenith)",
                  line=line[no], suggested="Ask the vendor to confirm the unit basis.")
        for no in (14, 15, 16):
            cid = _claim(conn, rfx, vendor, line[no], None, None, "rates as per our last order",
                         rule="rates as per our last order")
            conn.execute(db.cell.insert().values(
                rfx_id=rfx, vendor_id=vendor, rfx_line_id=line[no], state="inferred",
                claim_id=cid, unit_price_inr="12.4"))
            _flag(conn, rfx, vendor, "reference", "price taken from this vendor's own last "
                  "record (\"rates as per our last order\")", line=line[no], claim=cid)
        _flag(conn, rfx, vendor, "missing_info", "landed price unknown: freight amount not "
              "stated ('Freight extra at actuals.'); excluded from landed comparisons",
              suggested="Ask the vendor for the freight charge.")
        for no, cands in ((17, ("41.50", "44.50")), (18, ("7.10", "7.70")),
                          (19, ("2.30", "2.80"))):
            cid = _claim(conn, rfx, vendor, line[no], cands[0], "each", f"W{no} {cands[0]}")
            _flag(conn, rfx, vendor, "read_mismatch", "two reads disagree", line=line[no],
                  claim=cid, cands=cands)
        _flag(conn, rfx, vendor, "spec_deviation", "quoted item differs", line=line[20])
        _flag(conn, rfx, other, "plausibility", "far below peer median", line=line[1])
    return {"rfx": rfx, "vendor": vendor, "lines": line}


def test_thirteen_lines_with_one_unit_issue_make_one_question(event):
    d = clarify.draft(event["rfx"], event["vendor"], use_model=False)
    unit = [g for g in d.groups if g.kind.value == "plausibility"]
    assert len(unit) == 1 and unit[0].lines == list(range(1, 14))
    assert "Lines: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13" in d.body
    assert d.body.count('"per bundle"') == 1  # asked once, in the vendor's own words


def test_groups_ordered_by_money_at_stake_and_capped_at_five(event):
    d = clarify.draft(event["rfx"], event["vendor"], use_model=False)
    # 1 unit + 1 reference + 1 freight + 3 hard-to-read prices = 6 groups; 5 asked
    assert len(d.groups) == clarify.MAX_QUESTIONS and d.held_back == 1
    assert d.groups[0].kind.value == "plausibility"  # most money at stake comes first
    assert [g.stake for g in d.groups] == sorted((g.stake for g in d.groups), reverse=True)


def test_email_has_the_guide_shape_and_passes_the_checks(event):
    d = clarify.draft(event["rfx"], event["vendor"], use_model=False)
    assert d.subject == "Annual contract for widgets — 5 points to confirm on your quotation"
    assert d.body.startswith("Dear Sir/Madam,\n\nThank you for your quotation dated "
                             "10 Nov 2026.")
    assert "A short reply to this email is enough" in d.body
    assert d.body.rstrip().endswith("Example Industries Ltd")
    assert clarify.check_email(d.body, d.groups, set(range(1, 21))) == []
    assert copy.find_banned(d.body, copy.BANNED_EMAIL) == []


def test_reference_lines_list_the_prices_taken(event):
    d = clarify.draft(event["rfx"], event["vendor"], use_model=False)
    assert "Line 14: ₹12.40 per piece" in d.body
    # lines listed one by one with their prices are not listed again (QA P3-M1)
    assert "Lines: 14, 15, 16" not in d.body


def test_never_another_vendors_data_or_a_peer_price(event):
    d = clarify.draft(event["rfx"], event["vendor"], use_model=False)
    assert "Zenith" not in d.body and "4.10" not in d.body and "median" not in d.body


def test_only_vendor_closable_issues_are_asked(event):
    d = clarify.draft(event["rfx"], event["vendor"], use_model=False)
    assert all(g.kind.value != "spec_deviation" for g in d.groups)


def test_check_email_catches_repeats_banned_words_and_unknown_lines(event):
    d = clarify.draft(event["rfx"], event["vendor"], use_model=False)
    bad = d.body.replace("2. ", "1. ", 1)
    assert "each group must appear exactly once" in clarify.check_email(bad, d.groups,
                                                                        set(range(1, 21)))
    assert any("banned" in p for p in clarify.check_email(d.body + "\nThere is an error.",
                                                          d.groups, set(range(1, 21))))
    assert any("not in the RFx" in p for p in clarify.check_email(d.body, d.groups,
                                                                  set(range(1, 10))))


def _stub_model(monkeypatch, make):
    """Replace the model call with a reply built from the numbered questions it was sent."""
    def structured(model, messages, **kwargs):
        n = len(re.findall(r"^\d+\. ", messages[0]["content"], re.M))
        return SimpleNamespace(parsed=clarify.Wording(questions=make(n)))
    monkeypatch.setattr(llm, "structured", structured)


def test_model_wording_is_used_when_it_passes_the_check(event, monkeypatch):
    _stub_model(monkeypatch, lambda n: [
        clarify.WordedQuestion(group=i, text=f"Could you confirm point {'abcde'[i - 1]}?")
        for i in range(1, n + 1)])
    d = clarify.draft(event["rfx"], event["vendor"])
    assert d.worded_by == "model" and "1. Could you confirm point a?" in d.body
    assert "Lines: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13" in d.body  # code adds lines


@pytest.mark.parametrize("make", [
    lambda n: [clarify.WordedQuestion(group=i, text="Is there a mismatch?")
               for i in range(1, n + 1)],                       # a banned word
    lambda n: [clarify.WordedQuestion(group=1, text="All fine?")],  # groups dropped
    lambda n: [clarify.WordedQuestion(group=i, text="Is it 999 per bundle?")
               for i in range(1, n + 1)],                       # a figure not in the facts
])
def test_model_wording_that_fails_the_check_falls_back_to_fixed(event, monkeypatch, make):
    _stub_model(monkeypatch, make)
    d = clarify.draft(event["rfx"], event["vendor"])
    assert d.worded_by == "fixed" and "999" not in d.body and "mismatch" not in d.body


def test_no_api_key_keeps_the_fixed_wording(event, monkeypatch):
    monkeypatch.delenv("LEVEL_ANTHROPIC_API_KEY", raising=False)
    d = clarify.draft(event["rfx"], event["vendor"])
    assert d.worded_by == "fixed" and d.body.count("Lines:") >= 1
