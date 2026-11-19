"""Review page redesign: price context, action labels, same-cause bulk accept, undo, and the
buyer-only rule for the new context data (QA usability checks a-c).

The event is the small safety-supplies RFx from pipeline_helpers. Vendor North quotes line 1
and says "rest same as last year" for lines 2, 3 and 4, which resolve from North's own FY26
records. Vendor South quotes every line; South's prices, the median and River's contract
price are buyer-only context and must never reach North.
"""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from components import data, review_data

from level import copy, db
from level.agents.vendor_copilot import agent, link, questions
from level.core import review as core
from level.pipeline import clarify
from level.pipeline import run as pipeline_run
from level.types import FlagType, Severity
from tests import pipeline_helpers as H

ROOT = Path(__file__).resolve().parents[1]
SOUTH_PRICES = {1: "222.22", 2: "4.44", 3: "155.55", 4: "0.95"}
NORTH_RECORDS = {2: "4.10", 3: "151", 4: "0.88"}  # North's own records (quotes) for FY26


def _claim(conn, rfx, vendor, lines, price, per, quote, scope="line", rule=None):
    cid = conn.execute(db.quote_claim.insert().values(
        rfx_id=rfx, vendor_id=vendor, vendor_item_text=quote, raw_price=price,
        raw_currency="INR" if price else None, basis={"per": per, "multiple": None}
        if per else None, scope=scope, rule_text=rule, evidence_locator="email, line 1",
        evidence_quote=quote, evidence_ok=True)).inserted_primary_key[0]
    for line in lines:
        conn.execute(db.claim_line.insert().values(claim_id=cid, rfx_line_id=line))
    return cid


@pytest.fixture
def event(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LEVEL_LINK_SECRET", "test-secret")
    ids = H.seed()
    rfx, lines, v = ids["rfx"], ids["lines"], ids["vendors"]
    north, south = v["North"], v["South"]
    with db.begin() as conn:
        conn.execute(db.invitation.update().where(db.invitation.c.vendor_id.in_(
            [north, south])).values(status="replied"))
        for vendor, body in ((north, "Helmet 205 each, rest same as last year"),
                             (south, "South quote")):
            conn.execute(db.message.insert().values(
                rfx_id=rfx, vendor_id=vendor, direction="in", channel="email", body=body,
                received_at="2026-01-01T00:00:00.000000Z"))
        for no, price in NORTH_RECORDS.items():
            conn.execute(db.price_history.insert().values(
                vendor_id=north, rfx_line_id=lines[no], period="FY26", unit_price=price,
                currency="INR", uom=H.LINES[no - 1][2], source="quote"))
        _claim(conn, rfx, north, [lines[1]], "205", "each", "Helmet 205 each")
        ref = _claim(conn, rfx, north, [lines[2], lines[3], lines[4]], None, None,
                     "rest same as last year", scope="reference",
                     rule="rest same as last year")
        for no in (2, 3, 4):
            conn.execute(db.flag.insert().values(
                rfx_id=rfx, vendor_id=north, target_kind="claim", target_id=ref,
                rfx_line_id=lines[no], type=FlagType.REFERENCE.value,
                severity=Severity.INFO.value, state="open",
                reason='price taken from this vendor\'s own last record '
                       '("rest same as last year")',
                suggested_action="Ask the vendor to confirm the price."))
        for no, price in SOUTH_PRICES.items():
            unit = H.LINES[no - 1][2]
            _claim(conn, rfx, south, [lines[no]], price, unit, f"item {no} {price}")
        for vendor in (north, south):
            conn.execute(db.vendor_term.insert().values(
                rfx_id=rfx, vendor_id=vendor, type="freight", text="freight included",
                is_conditional=False, evidence_ok=True))
    pipeline_run.post_checks(rfx)
    return SimpleNamespace(rfx=rfx, lines=lines, north=north, south=south)


def _north(event):
    return [i for i in review_data.issues(event.rfx) if i.flag.vendor_id == event.north]


# --- (a) every price issue shows last year's record and this year's comparison prices ------


def test_every_price_issue_shows_own_record_and_this_years_prices(event):
    items = review_data.issues(event.rfx)
    price_items = [i for i in items if i.is_price]
    assert price_items
    for i in price_items:
        ctx = review_data.price_context(event.rfx, i)
        assert ctx is not None and "own" in ctx and "peers" in ctx and "median" in ctx
        others = [p["Vendor"] for p in ctx["peers"]]
        assert i.vendor not in others  # the vendor's own price is not its own comparison
    ref = next(i for i in _north(event) if i.line_no == 3)
    ctx = review_data.price_context(event.rfx, ref)
    assert ctx["own"].startswith("₹151.00 per each") and "quote" in ctx["own"]
    assert "FY26" in ctx["own"]
    assert [p["Vendor"] for p in ctx["peers"]] == ["South"]
    assert ctx["median"] == "₹155.55"
    assert "River" in ctx["last_contract"] and "₹149.00" in ctx["last_contract"]


def test_price_issue_without_a_record_says_so_not_zero(event):
    with db.begin() as conn:
        conn.execute(db.price_history.delete().where(
            db.price_history.c.vendor_id == event.south))
    south = [i for i in review_data.issues(event.rfx)
             if i.flag.vendor_id == event.south and i.is_price]
    for i in south:
        assert review_data.price_context(event.rfx, i)["own"] is None


def test_record_date_comes_from_the_past_event(event):
    with db.begin() as conn:
        conn.execute(db.rfx.insert().values(
            title="Last year", status="awarded", base_currency="INR",
            terms={"period": "FY26", "due": "2025-02-20", "award_date": "2025-03-10"}))
    ref = next(i for i in _north(event) if i.line_no == 3)
    assert "quoted by 20 Feb 2025" in review_data.price_context(event.rfx, ref)["own"]


# --- (b) every action button names its outcome and amount ---------------------------------


def test_action_labels_name_the_outcome_and_the_amount(event):
    for i in review_data.issues(event.rfx):
        accept = copy.accept_label(i.flag.type, i.price, i.is_price, i.history_source,
                                   i.flag.reason)
        assert accept not in ("Accept price", "OK", "Submit", "Accept")
        if i.is_price and i.price is not None:
            assert copy.unit_price(i.price) in accept
        if i.is_price:
            assert str(i.line_no) in copy.not_quoted_label(i.line_no)
            assert str(i.line_no) in copy.save_price_label(i.line_no)
        assert i.vendor in copy.ask_label(i.vendor)
        assert not copy.find_banned(accept)
    ref = next(i for i in _north(event) if i.line_no == 3)
    assert copy.accept_label(ref.flag.type, ref.price, True, ref.history_source) == \
        "Accept ₹151.00 (their quote last year)"


def test_row_summary_names_the_line_and_the_value(event):
    ref = next(i for i in _north(event) if i.line_no == 3)
    assert ref.summary == "Line 3 · ₹151.00 taken from last year's quote"
    assert "rest same as last year" in ref.found and "₹151.00" in ref.found
    for i in review_data.issues(event.rfx):
        assert i.summary and not copy.find_banned(i.summary + " " + i.found)


# --- (c) same-cause issues for one vendor clear in one confirmed action --------------------


def test_same_cause_issues_clear_in_one_action_and_undo_together(event):
    items = review_data.issues(event.rfx)
    ref = next(i for i in items if i.flag.vendor_id == event.north and i.line_no == 3)
    group = review_data.same_cause(items, ref)
    assert [g.line_no for g in group] == [2, 3, 4]
    assert [g.price for g in group] == [Decimal(p) for p in NORTH_RECORDS.values()]

    data.resolve_flags([g.flag.id for g in group], "confirm")
    after = {i.key: i for i in review_data.issues(event.rfx)}
    assert all(after[g.key].decided for g in group)
    with db.connect() as conn:
        audit = conn.execute(sa.select(db.audit_event).where(
            db.audit_event.c.action == "flag.confirm")).mappings().all()
    assert len(audit) == 3 and len({a["after"]["batch"] for a in audit}) == 1
    cells = {c["rfx_line_id"]: c for c in data._rows(sa.select(db.cell).where(
        db.cell.c.vendor_id == event.north))}
    for g in group:  # accepting keeps the price; nothing about the value changes
        assert Decimal(cells[g.flag.rfx_line_id]["unit_price_inr"]) == g.price

    last = data.last_decision(event.rfx)
    assert last["action"] == "confirm" and sorted(last["flag_ids"]) == sorted(
        g.flag.id for g in group)
    assert sorted(data.undo_last(event.rfx)) == sorted(g.flag.id for g in group)
    reopened = {i.key: i for i in review_data.issues(event.rfx)}
    assert not any(reopened[g.key].decided for g in group)
    assert data.last_decision(event.rfx) is None  # one undo, not a history walk


def test_different_causes_are_not_grouped(event):
    items = review_data.issues(event.rfx)
    for i in items:
        group = review_data.same_cause(items, i)
        assert all(g.flag.type == i.flag.type and g.flag.vendor_id == i.flag.vendor_id
                   for g in group)
        assert len(group) != 1


def test_undo_of_a_single_decision_reopens_it(event):
    ref = next(i for i in _north(event) if i.line_no == 2)
    data.resolve_flag(ref.flag.id, "not_quoted")
    assert data.last_decision(event.rfx)["flag_ids"] == [ref.flag.id]
    data.undo_last(event.rfx)
    again = next(i for i in _north(event) if i.line_no == 2)
    assert not again.decided
    cell = data._rows(sa.select(db.cell).where(db.cell.c.vendor_id == event.north,
                                               db.cell.c.rfx_line_id == event.lines[2]))[0]
    assert cell["state"] != "not_quoted" and Decimal(cell["unit_price_inr"]) == Decimal("4.10")


# --- Buyer only: the new context data never reaches a vendor (non-negotiable 10) ----------


def test_price_context_never_reaches_the_vendor(event):
    north_items = _north(event)
    secrets = {"South", "River", "155.55", "222.22", "149"}
    for i in north_items:
        ctx = review_data.price_context(event.rfx, i)
        if ctx and ctx["median"]:
            secrets.add(ctx["median"].lstrip("₹"))
    scope = link.LinkScope(event.rfx, event.north)
    vendor_texts = [
        clarify.draft(event.rfx, event.north, use_model=False).body,
        json.dumps(questions.as_dicts(questions.open_questions(scope))),
        json.dumps(questions.rfx_summary(scope), default=str),
        json.dumps(questions.quote_summary(scope), default=str),
        agent.context(scope),
    ]
    for text in vendor_texts:
        assert not [s for s in secrets if s in text], text


def test_vendor_facing_code_never_imports_the_review_context():
    vendor_facing = [ROOT / "app/views/8_Vendor_Reply.py", ROOT / "level/pipeline/clarify.py",
                     *sorted((ROOT / "level/agents/vendor_copilot").glob("*.py"))]
    for path in vendor_facing:
        text = path.read_text(encoding="utf-8")
        assert "review_data" not in text and "core.review" not in text, path
        assert not re.search(r"from level\.core import .*\breview\b", text), path


# --- Pure helpers ---------------------------------------------------------------------------


def test_queue_order_and_navigation():
    items = [core.QueueItem("a", 1, Decimal(10), 2, 1), core.QueueItem("b", 2, Decimal(500), 2, 2),
             core.QueueItem("c", 1, Decimal(0), 0, 3), core.QueueItem("d", 1, Decimal(300), 1, 4)]
    keys = [i.key for i in core.order_queue(items)]
    assert keys == ["b", "d", "a", "c"]  # vendor 2 (500) before vendor 1 (310)
    assert core.position(keys, "a") == (3, 4)
    assert core.step(keys, "b", -1) is None and core.step(keys, "b", 1) == "d"
    assert core.next_open(keys, {"b", "c"}, "c") == "b"  # wraps to the top
    assert core.next_open(keys, {"c"}, "c") is None


def test_price_context_prefers_own_contract_and_skips_own_price_in_median():
    hist = [core.Record(1, Decimal("10"), "quote", "FY25"),
            core.Record(1, Decimal("9"), "contract", "FY25"),
            core.Record(2, Decimal("8"), "contract", "FY25")]
    peers = [core.PeerPrice(1, Decimal("100"), "confirmed"),
             core.PeerPrice(2, Decimal("4"), "confirmed"),
             core.PeerPrice(3, Decimal("6"), "inferred")]
    ctx = core.price_context(1, Decimal("100"), hist, peers)
    assert ctx.own_record.price == Decimal("9")
    assert ctx.peer_median == Decimal("5") and ctx.vs_median == Decimal("20.0")
    assert [p.vendor_id for p in ctx.peers] == [2, 3]
    assert ctx.last_contract.vendor_id == 2
    assert core.price_context(1, None, [], []).peer_median is None
    assert core.stake(None, Decimal(5)) == 0 and core.stake(Decimal(2), Decimal(5)) == 10


def test_deadline_is_relative_to_today():
    now = datetime(2025, 2, 27, 10, tzinfo=UTC)
    assert copy.deadline("2025-02-24", now) == "Replies closed 24 Feb 2025 · 3 days ago"
    assert copy.deadline("2025-03-01", now) == "Replies due 1 Mar 2025 · closes in 2 days"
    assert copy.deadline("2025-02-27", now) == "Replies due 27 Feb 2025 · closes today"
    assert copy.deadline("2023-02-24", now).endswith("· 2 years ago")
    assert copy.deadline(None) == "No reply date set"
