"""WP13: past events on the RFx page, "Start from this event", and the buyer-only reference.

No model calls. Names and prices here are made up for the test, not taken from the dataset.
"""

import io
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from openpyxl import load_workbook

from level import copy, db
from level.agents.copilot import agent, draft
from level.agents.vendor_copilot import agent as vendor_agent
from level.agents.vendor_copilot.link import LinkScope
from level.agents.vendor_copilot.questions import rfx_summary
from level.core.past import award_value

PAST_PRICE = "987.65"  # distinctive, so a leak is easy to spot


@pytest.fixture
def past(tmp_db, tmp_path, monkeypatch):
    """An awarded event, recorded with one supplier per line, and two vendors."""
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    with db.begin() as conn:
        winner = conn.execute(db.vendor.insert().values(
            name="Zenith Supplies", email="z@x")).inserted_primary_key[0]
        other = conn.execute(db.vendor.insert().values(
            name="Orbit Traders", email="o@x")).inserted_primary_key[0]
        old = conn.execute(db.rfx.insert().values(
            title="Office chairs, FY25", category="Furniture", status="awarded",
            issued_at="2024-02-05T00:00:00.000000Z",
            terms={"ref": "OFF/FY25/1", "period": "FY25", "award_date": "2024-03-15",
                   "price_basis": "Delivered", "currency": "INR", "payment_days": "45",
                   "delivery": "To head office"})).inserted_primary_key[0]
    draft.upsert_lines(old, [
        {"line_no": 1, "description": "Task chair", "canonical_uom": "each",
         "annual_qty": "200", "spec": {"arms": "fixed"}},
        {"line_no": 2, "description": "Stool", "canonical_uom": "each", "annual_qty": "50",
         "spec": {"height": "65 cm"}},
    ])
    draft.upsert_questions(old, [{"q_no": 1, "section": "Quality", "text": "Warranty years?",
                                  "is_gating": True, "pass_rule": {"op": ">=", "value": "3"}}])
    draft.set_invited(old, [winner, other])
    with db.begin() as conn:
        line_ids = dict(conn.execute(sa.select(db.rfx_line.c.line_no, db.rfx_line.c.id).where(
            db.rfx_line.c.rfx_id == old)).all())
        conn.execute(db.award_line.insert(), [
            {"rfx_id": old, "rfx_line_id": line_ids[1], "vendor_id": winner,
             "unit_price": PAST_PRICE},
            {"rfx_id": old, "rfx_line_id": line_ids[2], "vendor_id": other,
             "unit_price": "100.00"},
        ])
    return SimpleNamespace(old=old, winner=winner, other=other)


def _past_rows(rfx_id: int) -> dict:
    with db.connect() as conn:
        return {
            "rfx": dict(conn.execute(sa.select(db.rfx).where(db.rfx.c.id == rfx_id))
                        .mappings().one()),
            "lines": [dict(r) for r in conn.execute(sa.select(db.rfx_line).where(
                db.rfx_line.c.rfx_id == rfx_id).order_by(db.rfx_line.c.id)).mappings()],
            "questions": [dict(r) for r in conn.execute(sa.select(db.question).where(
                db.question.c.rfx_id == rfx_id)).mappings()],
            "invited": sorted(draft.invited_ids(rfx_id)),
        }


# --- Award value (level/core) ------------------------------------------------------------


def test_award_value_sums_price_times_quantity():
    total, left_out = award_value([{"unit_price": "987.65", "annual_qty": "200"},
                                   {"unit_price": "100.00", "annual_qty": "50"}])
    assert total == Decimal("202530.00") and left_out == 0


def test_award_value_never_counts_a_missing_price_as_zero():
    total, left_out = award_value([{"unit_price": None, "annual_qty": "10"},
                                   {"unit_price": "5", "annual_qty": None}])
    assert total is None and left_out == 2
    total, left_out = award_value([{"unit_price": "2", "annual_qty": "3"},
                                   {"unit_price": None, "annual_qty": "10"}])
    assert total == Decimal("6") and left_out == 1


# --- The list and the read-only view -----------------------------------------------------


def test_past_events_list_shows_the_award(past):
    [event] = draft.past_events()
    assert event["number"] == "OFF/FY25/1" and event["year"] == "FY25"
    assert event["status"] == "awarded" and event["invited"] == 2
    assert event["suppliers"] == ["Zenith Supplies", "Orbit Traders"]
    assert event["award_value"] == Decimal("202530.00")  # 987.65 x 200 + 100 x 50
    assert event["award_date"] == "2024-03-15"
    assert draft.record_only_ids() == {past.old}


def test_drafts_are_not_past_events(past):
    rid = draft.new_draft()
    assert [e["id"] for e in draft.past_events(exclude_rfx_id=rid)] == [past.old]
    assert rid not in {s["id"] for s in draft.history_sources(past.old)}


def test_past_event_detail_is_read_only_data(past):
    before = _past_rows(past.old)
    detail = draft.past_event(past.old)
    assert [ln["awarded_to"] for ln in detail["lines"]] == ["Zenith Supplies", "Orbit Traders"]
    assert detail["lines"][0]["awarded_price"] == PAST_PRICE
    assert "ref" not in detail["terms"] and detail["terms"]["payment_days"] == "45"
    assert _past_rows(past.old) == before


def test_an_event_awarded_in_level_shows_its_chosen_option(past):
    """Events awarded on the Award page keep their award as an award option, not a record."""
    with db.begin() as conn:
        conn.execute(db.award_line.delete())
        line_id = conn.execute(sa.select(db.rfx_line.c.id).where(
            db.rfx_line.c.rfx_id == past.old, db.rfx_line.c.line_no == 1)).scalar()
        result = {"params": {"strategy": "cheapest_per_line"}, "vendors_used": [past.other],
                  "allocation": [{"rfx_line_id": line_id, "vendor_id": past.other,
                                  "unit_price_inr": "10", "annual_qty": "200",
                                  "line_total_inr": "2000", "cell_state": "confirmed"}],
                  "total_inr": "2000", "last_year_total_inr": None, "savings_inr": None,
                  "savings_pct": None, "coverage_gaps": [], "caveats": [], "sensitivity": []}
        conn.execute(db.scenario.insert().values(rfx_id=past.old, params=result["params"],
                                                 result=result, is_award=True, pinned=True))
    [event] = draft.past_events()
    assert event["suppliers"] == ["Orbit Traders"] and event["award_value"] == Decimal("2000")


# --- Start from this event ---------------------------------------------------------------


def test_start_from_this_event_copies_and_leaves_the_source_alone(past):
    before = _past_rows(past.old)
    rid = draft.new_draft()
    out = draft.import_from_history(rid, source_rfx_id=past.old)
    assert (out["imported"], out["questions"], out["invited_vendors"]) == (2, 1, 2)
    assert out["source_number"] == "OFF/FY25/1"
    snap = draft.snapshot(rid)
    assert [ln["description"] for ln in snap["lines"]] == ["Task chair", "Stool"]
    assert snap["questions"][0]["pass_rule"] == {"op": ">=", "value": "3"}
    assert snap["terms"] == {"ref": "OFF/FY25/2",  # the buyer's numbering, continued
                             "price_basis": "Delivered", "currency": "INR",
                             "payment_days": "45", "delivery": "To head office"}
    assert sorted(draft.invited_ids(rid)) == sorted([past.winner, past.other])
    # "(copy)" until renamed, so two events never share a name (UX audit H10)
    assert snap["title"] == "Office chairs, FY25 (copy)" and snap["status"] == "draft"
    assert _past_rows(past.old) == before  # the past event is never modified
    assert "Started from OFF/FY25/1" in copy.started_from(out)


def test_start_from_keeps_what_the_draft_already_has(past):
    rid = draft.new_draft("Chairs FY26")
    draft.upsert_lines(rid, [{"line_no": 1, "description": "Mesh chair", "canonical_uom": "each"}])
    draft.set_terms(rid, {"payment_days": "60"})
    out = draft.import_from_history(rid, source_rfx_id=past.old)
    snap = draft.snapshot(rid)
    assert out["imported"] == 1 and out["skipped_existing"] == 1
    assert snap["lines"][0]["description"] == "Mesh chair"
    assert snap["terms"]["payment_days"] == "60" and snap["title"] == "Chairs FY26"
    assert draft.import_from_history(rid, source_rfx_id=past.old)["imported"] == 0  # 2nd click


def test_copilot_tool_and_button_share_one_code_path(past, monkeypatch):
    seen = []
    monkeypatch.setattr(draft, "import_from_history",
                        lambda rfx_id, source_rfx_id=None, actor="buyer": seen.append(actor))
    agent.run_tool(1, "import_from_history", {})
    assert seen == ["copilot"]


def test_copilot_default_names_the_event_it_used(past):
    rid = draft.new_draft()
    out = agent.run_tool(rid, "import_from_history", {})
    assert out["source_number"] == "OFF/FY25/1" and out["imported"] == 2


# --- Non-negotiable 10: last year's price and supplier stay with the buyer --------------


def test_reference_price_is_buyer_only(past):
    rid = draft.new_draft()
    draft.import_from_history(rid, source_rfx_id=past.old)
    ref = draft.reference_prices(rid)
    assert ref[1] == {"price": PAST_PRICE, "supplier": "Zenith Supplies"}  # the page shows it

    # The draft snapshot (what the email, template and co-pilot are built from) has no price
    assert PAST_PRICE not in json.dumps(draft.snapshot(rid))
    # The email and the Excel template: no price, no supplier
    subject, body = draft.render_email(rid)
    wb = load_workbook(io.BytesIO(draft.template_xlsx(rid)))
    xlsx_text = " ".join(str(c.value) for ws in wb for row in ws.iter_rows() for c in row)
    for text in (subject + body, xlsx_text):
        assert PAST_PRICE not in text and "Zenith" not in text and "Orbit" not in text

    # The emails actually sent, one per vendor, with the reply link
    draft.set_terms(rid, {"due": "2026-12-01"})
    assert draft.send(rid) == 2
    with db.connect() as conn:
        sent = conn.execute(sa.select(db.message.c.vendor_id, db.message.c.body).where(
            db.message.c.rfx_id == rid, db.message.c.direction == "out")).all()
    assert len(sent) == 2
    for _, sent_body in sent:
        assert PAST_PRICE not in sent_body and "Zenith" not in sent_body

    # The vendor reply link: what the page shows and what its co-pilot sees
    scope = LinkScope(rfx_id=rid, vendor_id=past.other)
    for text in (json.dumps(rfx_summary(scope), default=str), vendor_agent.context(scope)):
        assert PAST_PRICE not in text and "Zenith" not in text


def test_existing_database_gets_the_new_table_on_start(tmp_db):
    """Databases made before WP13 keep working: db.init() adds award_line, no reset."""
    with db.begin() as conn:
        conn.exec_driver_sql("DROP TABLE award_line")
    db.init()
    assert draft.record_only_ids() == set() and draft.past_events() == []
