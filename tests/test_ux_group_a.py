"""UX audit, Group A fixes: wording on the RFx and vendor pages, and which event the pages show.

No model calls. Names and figures are made up for the test, not taken from the dataset.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from components import data

from level import copy, db
from level.agents.copilot import draft, rfx_doc

ROOT = Path(__file__).resolve().parent.parent
RAW = re.compile(r"[{}\[\]]|\b[a-z]+_[a-z_]+\b|Decimal\(|\bNone\b")  # dicts, lists, keys


# --- B2: the vendor page shows terms and specs as words -----------------------------------


def test_terms_are_labelled_words_not_a_dict():
    terms = {"ref": "ABC/FY26/7", "title": "Chairs", "issued": "2025-02-03",
             "plants": ["North", "East"], "payment_days_requested": 60,
             "basis": "per piece, ex-GST"}
    rows = copy.term_rows(terms)
    text = " ".join(f"{r['Term']} {r['Value']}" for r in rows)
    assert "3 Feb 2025" in text and "North, East" in text and "Payment terms (days) 60" in text
    assert "Chairs" not in text  # the title is the page heading already
    assert not RAW.search(text), text


def test_spec_reads_as_one_line_with_a_size():
    spec = {"style": "A", "length_mm": 430, "width_mm": 240, "height_mm": 190,
            "board_gsm": "150/120", "sku": "X-1"}
    text = copy.spec_text(spec)
    assert text == "Style A · Size 430 × 240 × 190 mm · Board (gsm) 150/120 · SKU X-1"
    assert copy.conversion_text({"g_per_piece": "454"}) == "454 g per piece"
    # no height: nothing is merged and nothing is dropped
    assert copy.spec_text({"length_mm": 380, "width_mm": 270}) == "Length (mm) 380 · Width (mm) 270"


def test_vendor_page_never_writes_a_raw_object():
    source = (ROOT / "app/views/8_Vendor_Reply.py").read_text(encoding="utf-8")
    assert "st.write(" not in source and "st.json(" not in source


# --- B7: the RFx tabs use words ------------------------------------------------------------


@pytest.mark.parametrize("words, rule", [
    ("10 or less", {"op": "<=", "value": "10"}), ("5 or more", {"op": ">=", "value": "5"}),
    ("Must be yes", {"op": "==", "value": "yes"}),
    ("Valid on 14 Mar 2025", {"op": "valid_on", "value": "2025-03-14"}),
    ("Valid on the award date", {"op": "valid_on_date", "value": None}),
])
def test_pass_rules_are_written_and_read_in_words(words, rule):
    assert draft.format_rule(rule) == words
    assert draft.parse_rule(words) == rule


def test_a_rule_typed_badly_is_explained():
    with pytest.raises(ValueError, match="10 or less"):
        draft.parse_rule("about ten")


def test_rfx_page_shows_no_rule_syntax_or_raw_keys():
    source = (ROOT / "app/views/1_RFx.py").read_text(encoding="utf-8")
    for raw in ("`is_yes`", "`valid_on_date`", "name=value", '"term": k', "yes_no\","):
        assert raw not in source
    assert set(copy.ANSWER_TYPE_LABEL) == {"yes_no", "number", "date", "text", "file"}


def test_stored_rules_are_ones_the_requirement_check_reads(tmp_db):
    rid = draft.new_draft("Chairs")
    draft.set_terms(rid, {"award_date": "2026-03-14"})
    draft.upsert_questions(rid, [
        {"text": "Certified?", "section": "Q", "is_gating": True,
         "pass_rule": {"op": "is_yes", "value": None}},
        {"text": "Certificate valid?", "section": "Q", "is_gating": True,
         "pass_rule": {"op": "valid_on_date", "value": None}}])
    rules = [q["pass_rule"] for q in draft.snapshot(rid)["questions"]]
    assert rules == [{"op": "==", "value": "yes"}, {"op": "valid_on", "value": "2026-03-14"}]


# --- B10: sending never moves the pages to an empty event ---------------------------------


def _event(conn, title, status, ref=None):
    return conn.execute(db.rfx.insert().values(
        title=title, status=status, terms={"ref": ref} if ref else {},
        issued_at="2026-01-01T00:00:00Z")).inserted_primary_key[0]


def test_default_event_is_the_one_with_replies_not_the_newest_sent(tmp_db):
    with db.begin() as conn:
        working = _event(conn, "Chairs FY26", "collecting", "ABC/FY26/7")
        conn.execute(db.message.insert().values(rfx_id=working, direction="in",
                                                channel="email", body="our prices"))
        newer = _event(conn, "Desks FY26", "issued", "ABC/FY26/8")
    assert data.current_rfx(None)["id"] == working
    assert data.current_rfx(newer)["id"] == newer  # what the buyer picked always wins
    assert copy.event_choice(data.current_rfx(None)) == "ABC/FY26/7 · Collecting quotes"


def test_two_open_events_cannot_share_a_title(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    with db.begin() as conn:
        _event(conn, "Chairs FY26", "collecting")
        vid = conn.execute(db.vendor.insert().values(name="Acme")).inserted_primary_key[0]
    rid = draft.new_draft("Chairs FY26")
    draft.set_invited(rid, [vid])
    draft.set_terms(rid, {"due": "2026-12-01"})
    with pytest.raises(ValueError, match="title"):
        draft.send(rid)
    draft.set_header(rid, "Chairs FY27", None)
    assert draft.send(rid) == 1


def test_sending_needs_a_reply_date(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    with db.begin() as conn:
        vid = conn.execute(db.vendor.insert().values(name="Acme")).inserted_primary_key[0]
    rid = draft.new_draft("Chairs FY27")
    draft.set_invited(rid, [vid])
    assert any("reply-by date" in i for i in draft.check_completeness(rid))
    with pytest.raises(ValueError, match="reply-by date"):
        draft.send(rid)


def test_home_pins_the_event_for_the_session():
    source = (ROOT / "app/Home.py").read_text(encoding="utf-8")
    assert 'st.session_state["rfx_id"] = picked["id"]' in source
    assert 'url_path="overview", default=True' not in source  # M8: default page lives at "/"
    assert 'visibility="hidden"' in source  # and /overview forwards to it


# --- H10 / M14: past events, copied terms, numbering, the email ---------------------------


def test_only_finished_events_are_past_events_and_terms_map_to_current_names(tmp_db):
    with db.begin() as conn:
        _event(conn, "Chairs FY26", "collecting", "ABC/FY26/7")
        old = _event(conn, "Chairs FY25", "awarded", "ABC/FY25/3")
        conn.execute(db.rfx.update().where(db.rfx.c.id == old).values(terms={
            "ref": "ABC/FY25/3", "basis": "per piece, ex-GST", "payment_days_requested": 60,
            "plants": ["North"]}))
    for rid in (old, old - 1):  # both events have a line, so either could be copied
        draft.upsert_lines(rid, [{"line_no": 1, "description": "Chair", "canonical_uom": "each",
                                  "annual_qty": "10", "spec": {"arms": "fixed"}}])
    assert [e["id"] for e in draft.past_events()] == [old]
    assert draft.past_events()[0]["year"] == "FY25"  # from the event number
    rid = draft.new_draft()
    draft.import_from_history(rid)  # the co-pilot's default: the last awarded event
    terms = draft.snapshot(rid)["terms"]
    assert terms["price_basis"] == "per piece, ex-GST" and terms["payment_days"] == "60"
    assert terms["delivery"] == "To North" and "basis" not in terms
    assert not [i for i in draft.check_completeness(rid) if "is not stated" in i]
    assert draft.snapshot(rid)["title"] == "Chairs FY25 (copy)"


def test_a_new_draft_continues_the_buyers_numbering(tmp_db):
    with db.begin() as conn:
        _event(conn, "Chairs FY26", "collecting", "ABC/FY26/007")
    rid = draft.new_draft()
    assert draft.snapshot(rid)["terms"]["ref"] == "ABC/FY26/008"


def test_email_key_terms_are_each_said_once():
    lines = rfx_doc.key_terms({"basis": "per piece", "price_basis": "per piece",
                               "payment_days_requested": 60, "plants": ["North", "East"]})
    assert lines == ["Price basis: per piece", "Payment terms (days): 60",
                     "Delivery: To North, East"]


# --- M7: the stage chip --------------------------------------------------------------------


def test_stage_moves_to_evaluate_once_replies_close():
    rfx = {"status": "collecting", "terms": {"due": "2025-02-24"}}
    now = datetime(2025, 3, 1, tzinfo=UTC)
    assert copy.event_stage(rfx, now=now) == "Evaluate"
    assert copy.event_stage({**rfx, "terms": {"due": "2025-03-24"}}, now=now) == "Collecting"
    assert copy.event_stage({**rfx, "terms": {}}, all_replied=True, now=now) == "Evaluate"
    assert copy.event_stage({"status": "awarded", "terms": {}}, now=now) == "Awarded"
