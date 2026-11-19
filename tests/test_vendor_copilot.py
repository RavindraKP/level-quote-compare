"""WP11 vendor reply link: questions, answers, confirmation, and that nothing leaks.

The event is the small safety-supplies RFx from pipeline_helpers (not the evaluation
dataset). Vendor North quotes line 1 per each, line 3 "per box" (box size unknown) and
leaves lines 2 and 4 blank. Vendor South is the peer whose data must never reach North.
"""

import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from components import data

from level import db
from level.agents import vendor_copilot
from level.agents.copilot import draft, rfx_doc
from level.agents.vendor_copilot import agent, answers, link, questions
from level.pipeline import run as pipeline_run
from tests import pipeline_helpers as H

# Peer data and last year's prices: none of these may ever reach North.
PEER_SECRETS = ["South", "155.55", "777.77", "222.22", "313.13", "919.19"]


def _claim(conn, rfx, vendor, line, price, per, quote):
    cid = conn.execute(db.quote_claim.insert().values(
        rfx_id=rfx, vendor_id=vendor, vendor_item_text=quote, raw_price=price,
        raw_currency="INR", basis={"per": per, "multiple": None}, scope="line",
        evidence_locator="email, line 1", evidence_quote=quote, evidence_ok=True,
    )).inserted_primary_key[0]
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
        conn.execute(db.rfx_line.update().where(db.rfx_line.c.id == lines[1]).values(
            last_price="313.13"))  # last year's prices: never shown to a vendor
        conn.execute(db.rfx_line.update().where(db.rfx_line.c.id == lines[3]).values(
            last_price="919.19"))
        conn.execute(db.invitation.update().where(db.invitation.c.vendor_id.in_(
            [north, south])).values(status="replied"))
        for vendor, body in ((north, "North quote"), (south, "South quote")):
            conn.execute(db.message.insert().values(
                rfx_id=rfx, vendor_id=vendor, direction="in", channel="email", body=body,
                received_at="2026-01-01T00:00:00.000000Z"))
        _claim(conn, rfx, north, lines[1], "205", "each", "Helmet 205 each")
        _claim(conn, rfx, north, lines[3], "1450", "box", "Vest Rs 1450 per box")
        _claim(conn, rfx, south, lines[1], "222.22", "each", "helmet 222.22")
        _claim(conn, rfx, south, lines[3], "155.55", "each", "vest 155.55")
        for vendor in (north, south):
            conn.execute(db.vendor_term.insert().values(
                rfx_id=rfx, vendor_id=vendor, type="freight", text="freight included",
                is_conditional=False, evidence_ok=True))
    pipeline_run.post_checks(rfx)
    return SimpleNamespace(rfx=rfx, lines=lines, north=link.LinkScope(rfx, north),
                           south=link.LinkScope(rfx, south))


def _cell(scope, line_id):
    with db.connect() as conn:
        return conn.execute(sa.select(db.cell).where(
            db.cell.c.vendor_id == scope.vendor_id, db.cell.c.rfx_line_id == line_id)
        ).mappings().one()


def _price_questions(scope) -> list:
    """Open questions about prices: the questionnaire questions are tested on their own."""
    return [q for q in questions.open_questions(scope) if q.kind != "requirement"]


def _leaks(text: str) -> list[str]:
    return [s for s in PEER_SECRETS if s in text]


def _pdf_text(content: bytes) -> str:
    import pymupdf

    with pymupdf.open(stream=content, filetype="pdf") as doc:
        return " ".join(" ".join(page.get_text().split()) for page in doc)


def _note(event, reason: str) -> int:
    """A line-less reading note on North's quote, as the pipeline writes one."""
    with db.begin() as conn:
        return conn.execute(db.flag.insert().values(
            rfx_id=event.rfx, vendor_id=event.north.vendor_id, target_kind="message",
            type="missing_info", severity="info", state="open",
            reason=f"unclear in the source: {reason}")).inserted_primary_key[0]


def _flag_state(flag_id: int) -> str:
    with db.connect() as conn:
        return conn.execute(sa.select(db.flag.c.state).where(db.flag.c.id == flag_id)).scalar()


# --- The link -----------------------------------------------------------------------------


def test_token_names_one_vendor_and_cannot_be_forged(event):
    token = link.make_token(event.rfx, event.north.vendor_id)
    assert link.verify_token(token) == event.north
    rfx, vendor, sig = token.split("-")
    assert link.verify_token(f"{rfx}-{event.south.vendor_id}-{sig}") is None  # swapped vendor
    assert link.verify_token("garbage") is None and link.verify_token(None) is None
    assert link.link_url(event.rfx, event.north.vendor_id).endswith(f"/?t={token}")


# --- Questions ----------------------------------------------------------------------------


def test_per_box_price_and_two_blank_lines_yield_exactly_those_questions(event):
    qs = _price_questions(event.north)
    got = sorted((q.kind, tuple(q.line_nos)) for q in qs)
    assert got == [("missing_line", (2,)), ("missing_line", (4,)), ("unit", (3,))]
    unit_q = next(q for q in qs if q.kind == "unit")
    assert "1450" in unit_q.text  # the vendor's own figure is shown back to them
    assert not _leaks(json.dumps(questions.as_dicts(qs)))  # peer median never in the wording


def test_context_and_summary_hold_only_the_vendors_own_data(event):
    ctx = agent.context(event.north)
    assert not _leaks(ctx) and "last_price" not in ctx
    assert not _leaks(json.dumps(questions.rfx_summary(event.north), default=str))
    summary = questions.quote_summary(event.north)
    assert not _leaks(json.dumps(summary, default=str))
    assert {r["line_no"] for r in summary["lines"]} == {1, 2, 3, 4}  # own lines only


def test_rfx_document_and_email_hold_no_prices_incumbent_or_other_vendors(event):
    """Non-negotiable 10 for what is sent: the RFx PDF, the email body, the vendor download."""
    with db.begin() as conn:  # South is the incumbent; last year's prices are on the lines
        conn.execute(db.vendor.update().where(db.vendor.c.id == event.south.vendor_id)
                     .values(is_incumbent=True))
        conn.execute(db.rfx_line.update().where(db.rfx_line.c.rfx_id == event.rfx)
                     .values(incumbent_vendor_id=event.south.vendor_id))
    name, pdf = questions.rfx_document(event.north)
    text = _pdf_text(pdf)
    assert name.endswith(".pdf") and "Safety helmet" in text  # it is the request itself
    _, body = draft.render_email(event.rfx, link.link_url(event.rfx, event.north.vendor_id))
    for sent in (text, rfx_doc.rfx_html(event.rfx), body):
        assert not _leaks(sent)
        assert "incumbent" not in sent.lower() and "last year" not in sent.lower()
    for _, _, content in draft.attachments(event.rfx):  # the template too
        assert not _leaks(content.decode("latin-1"))


def test_agent_has_one_tool_and_no_way_to_read_data():
    assert [t["name"] for t in agent.TOOLS] == ["record_answer"]
    assert set(agent.TOOLS[0]["input_schema"]["properties"]) == {"question_id", "value", "unit",
                                                                  "places"}


def test_tool_cannot_touch_another_vendors_question_or_rows(event):
    with db.begin() as conn:  # a question only South has: an unclear point in its document
        conn.execute(db.flag.insert().values(
            rfx_id=event.rfx, vendor_id=event.south.vendor_id, target_kind="attachment",
            type="missing_info", severity="info", reason="unclear in the source: 777.77 MOQ",
            state="open"))
    north_ids = {q.id for q in questions.open_questions(event.north)}
    south_only = [q for q in questions.open_questions(event.south) if q.id not in north_ids]
    assert south_only, "South has a question of its own"
    for q in south_only:  # North's scope cannot answer South's questions
        with pytest.raises(answers.AnswerError):
            agent.run_tool(event.north, "record_answer",
                           {"question_id": q.id, "value": "NO_BID", "unit": None},
                           "not offering", None)
    with pytest.raises(answers.AnswerError):
        agent.run_tool(event.north, "read_vendors", {}, "", None)  # no other tool exists
    # Ids like "line-2" exist for both; the link's scope decides whose rows change.
    before = _cell(event.south, event.lines[2])
    out = agent.run_tool(event.north, "record_answer",
                         {"question_id": f"line-{event.lines[2]}", "value": "NO_BID",
                          "unit": None}, "We are not offering gloves.", None)
    assert not _leaks(json.dumps(out))
    assert _cell(event.south, event.lines[2])["state"] == before["state"]  # South untouched


def test_a_number_not_in_the_vendors_words_is_refused(event):
    with pytest.raises(answers.AnswerError, match="not in the supplier's own words"):
        answers.record_answer(event.north, f"line-{event.lines[4]}", "0.85", "each",
                              "Cable ties are 0.95 each")


# --- Answering, confirming, correcting ----------------------------------------------------


def _answer_all(event):
    n, lines = event.north, event.lines
    answers.record_answer(n, f"line-{lines[3]}", "10", "each", "A box has 10 vests.")
    answers.record_answer(n, f"line-{lines[2]}", "NO_BID", None, "Not offering gloves.")
    answers.record_answer(n, f"line-{lines[4]}", "0.85", "each", "Cable ties 0.85 each")


def test_answers_make_cells_vendor_confirmed_and_clear_the_review_queue(event):
    _answer_all(event)
    n, lines = event.north, event.lines
    vest = _cell(n, lines[3])
    assert vest["unit_price_inr"] == "145.0000"  # 1450 per box of 10, divided by code
    assert vest["confirmed_by"] == "vendor" and vest["state"] == "confirmed"
    gloves = _cell(n, lines[2])
    assert gloves["state"] == "not_quoted" and gloves["confirmed_by"] == "vendor"
    ties = _cell(n, lines[4])
    assert ties["unit_price_inr"] == "0.8500" and ties["confirmed_by"] == "vendor"
    with db.connect() as conn:
        quote = conn.execute(sa.select(db.quote_claim.c.evidence_quote).where(
            db.quote_claim.c.id == ties["claim_id"])).scalar()
    assert quote == "Cable ties 0.85 each"  # the receipt is the vendor's own message
    assert _price_questions(n) == []
    assert [f for f in data.open_flags(event.rfx) if f.vendor_id == n.vendor_id] == []


def test_confirm_marks_the_rest_and_correct_reopens_a_question(event):
    _answer_all(event)
    n, lines = event.north, event.lines
    assert _cell(n, lines[1])["confirmed_by"] == "checks"
    assert answers.confirm(n) >= 1
    assert _cell(n, lines[1])["confirmed_by"] == "vendor"
    status = {r["line_no"]: r["status"] for r in questions.quote_summary(n)["lines"]}
    assert status[1] == "confirmed by you" and status[2] == "not offered"

    answers.correct(n, lines[1])
    assert _cell(n, lines[1])["confirmed_by"] != "vendor"
    reopened = _price_questions(n)
    assert [(q.kind, q.line_nos) for q in reopened] == [("price", [1])]
    answers.record_answer(n, reopened[0].id, "199", "each", "Helmet is 199 each, sorry")
    assert _cell(n, lines[1])["unit_price_inr"] == "199.0000"
    assert _cell(n, lines[1])["confirmed_by"] == "vendor"


def test_leaving_midway_keeps_a_normal_reply_with_the_rest_open(event):
    n, lines = event.north, event.lines
    job_id = vendor_copilot.submit_reply(n, [], "Vests Rs 1450 per box")
    assert job_id
    answers.record_answer(n, f"line-{lines[3]}", "10", "each", "10 per box")
    inbox = [m for m in data.messages(event.rfx, "in") if m["vendor_id"] == n.vendor_id]
    assert any(m["channel"] == "reply_link" for m in inbox)  # a normal reply in the inbox
    left = _price_questions(n)
    assert sorted(q.line_nos[0] for q in left) == [2, 4]  # still open for the buyer


# --- The chat loop with a scripted model --------------------------------------------------


def _fake_llm(script, seen):
    def complete(messages, **kwargs):
        seen.append(json.dumps(messages, default=str) + json.dumps(kwargs, default=str))
        content = script.pop(0)
        stop = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
        text = "".join(b.text for b in content if b.type == "text")
        return SimpleNamespace(message=SimpleNamespace(content=content, stop_reason=stop),
                               text=text)
    return complete


def test_chat_turn_records_through_the_tool_and_never_sends_peer_data(event, monkeypatch):
    seen: list[str] = []
    script = [
        [SimpleNamespace(type="tool_use", id="t1", name="record_answer",
                         input={"question_id": f"line-{event.lines[3]}", "value": "10",
                                "unit": "each"})],
        [SimpleNamespace(type="text", text="Thanks. Line 2 has no price: not offering?")],
    ]
    monkeypatch.setattr(agent.llm, "complete", _fake_llm(script, seen))
    reply = agent.run_turn(event.north, "There are 10 vests in a box.")
    assert "Line 2" in reply
    assert _cell(event.north, event.lines[3])["unit_price_inr"] == "145.0000"
    assert seen and not any(_leaks(s) for s in seen)  # every request the model received
    assert agent.history(event.north)[-1]["role"] == "assistant"


def test_a_price_matching_could_not_place_is_asked_about_and_placed(event):
    n, lines = event.north, event.lines
    with db.begin() as conn:  # read from the quote, but matched to no line
        cid = conn.execute(db.quote_claim.insert().values(
            rfx_id=event.rfx, vendor_id=n.vendor_id, vendor_item_text="Item B", raw_price="0.85",
            raw_currency="INR", basis={"per": "each", "multiple": None}, scope="line",
            evidence_quote="Item B 0.85 each", evidence_ok=True)).inserted_primary_key[0]
    first = questions.open_questions(n)[0]
    assert (first.id, first.kind) == (f"claim-{cid}", "which_line")
    with pytest.raises(answers.AnswerError, match="no line 9"):
        answers.record_answer(n, first.id, "9", None, "that is line 9")
    answers.record_answer(n, first.id, "4", None, "Item B is your line 4")
    assert _cell(n, lines[4])["unit_price_inr"] == "0.8500"
    assert "line-4" not in {q.id for q in questions.open_questions(n)}  # no longer blank


def test_saved_note_is_written_by_code_and_compact():
    outs = [{"recorded": "unit", "lines": [2], "value": "100", "unit": "piece"}] + [
        {"recorded": "missing_line", "lines": [n], "value": "NO_BID", "unit": None}
        for n in (3, 4, 5, 9)]
    assert agent.saved_note(outs) == ("Saved: line 2: price is for 100 pieces; lines 3-5, 9: "
                                      "not offered.")
    price = [{"recorded": "price", "lines": [30], "value": "62", "unit": "piece",
              "currency": "INR"}]
    assert agent.saved_note(price) == "Saved: line 30 at ₹62.00 per piece."


def test_reference_price_is_asked_to_be_confirmed_not_requoted(event):
    """QA M17: a 'same as before' line shows the vendor's own earlier price to confirm."""
    n, lines = event.north, event.lines
    with db.begin() as conn:
        conn.execute(db.price_history.insert().values(
            vendor_id=n.vendor_id, rfx_line_id=lines[4], period="FY25", unit_price="0.91",
            currency="INR", uom="each", source="contract"))
        cid = conn.execute(db.quote_claim.insert().values(
            rfx_id=event.rfx, vendor_id=n.vendor_id, vendor_item_text="rest unchanged",
            raw_price=None, scope="reference", rule_text="rest unchanged",
            evidence_locator="email, line 2", evidence_quote="rest unchanged",
            evidence_ok=True)).inserted_primary_key[0]
        conn.execute(db.claim_line.insert().values(claim_id=cid, rfx_line_id=lines[4]))
        conn.execute(db.flag.insert().values(
            rfx_id=event.rfx, vendor_id=n.vendor_id, target_kind="claim", target_id=cid,
            rfx_line_id=lines[4], type="reference", severity="info", state="open",
            reason='price taken from this vendor\'s own last record ("rest unchanged")'))
    pipeline_run.post_checks(event.rfx)
    q = next(q for q in questions.open_questions(n) if q.line_ids == [lines[4]])
    assert q.kind == "confirm_price" and "₹0.91" in q.text
    answers.record_answer(n, q.id, "YES", None, "Yes, that price still applies.")
    cell = _cell(n, lines[4])
    assert cell["confirmed_by"] == "vendor" and cell["unit_price_inr"] == "0.9100"
    # UX audit H1: the buyer's grid shows it as verified by the vendor, not assumed.
    assert cell["state"] == "confirmed"


# --- Reading notes with no line, line-by-line confirm, follow-up when nothing is recorded --


def test_note_about_a_questioned_line_closes_with_that_answer(event):
    """A note quoting the vendor's own item words joins that line's question."""
    n, lines = event.north, event.lines
    note = _note(event, "'Vest Rs 1450 per box' does not say how many vests are in a box.")
    assert "note-" not in " ".join(q.id for q in questions.open_questions(n))  # not asked twice
    answers.record_answer(n, f"line-{lines[3]}", "10", "each", "A box has 10 vests.")
    assert _flag_state(note) == "closed"


def test_note_about_read_lines_is_answered_by_confirming_them(event):
    n, lines = event.north, event.lines
    note = _note(event, "'Helmet 205 each' gives no currency; INR was taken from the RFx.")
    q = next(q for q in questions.open_questions(n) if q.id == f"note-{note}")
    assert q.kind == "confirm_reading" and q.line_ids == [lines[1]]
    assert lines[1] not in questions.line_blockers(n)  # confirming the line is the answer
    assert answers.confirm(n, [lines[1]]) == 1
    assert _flag_state(note) == "closed"
    assert f"note-{note}" not in {q.id for q in questions.open_questions(n)}


def test_note_can_also_be_answered_yes_in_the_chat_without_confirming_lines(event):
    """UX audit H1: a chat "yes" settles the note but is not a tick on each line; the vendor
    confirms lines with the buttons on the summary."""
    n, lines = event.north, event.lines
    note = _note(event, "'Helmet 205 each' gives no currency; INR was taken from the RFx.")
    answers.record_answer(n, f"note-{note}", "yes", None, "Yes, rupees.")
    assert _flag_state(note) == "closed"
    assert _cell(n, lines[1])["confirmed_by"] != "vendor"


def test_confirm_by_line_skips_lines_with_an_open_question(event):
    n, lines = event.north, event.lines
    blockers = questions.line_blockers(n)
    assert set(blockers) == {lines[2], lines[3], lines[4]}
    assert blockers[lines[3]].startswith("question ")
    assert answers.confirm(n, [lines[1], lines[3]]) == 1  # line 3 waits for its answer
    status = {r["line_no"]: r for r in questions.quote_summary(n)["lines"]}
    assert status[1]["status"] == "confirmed by you"
    assert status[3]["status"] == "question open" and status[3]["blocked_by"]
    assert not status[3]["can_confirm"]


def test_answering_another_note_does_not_hide_the_freight_question(event):
    n = event.north
    with db.begin() as conn:  # North did not state freight after all
        conn.execute(db.vendor_term.delete().where(db.vendor_term.c.vendor_id == n.vendor_id))
    pipeline_run.post_checks(event.rfx)
    assert "freight" in {q.id for q in questions.open_questions(n)}
    note = _note(event, "the quote has no validity date.")
    answers.record_answer(n, f"flag-{note}", "valid 90 days", None, "valid 90 days")
    answers.record_answer(n, f"line-{event.lines[3]}", "10", "each", "10 per box")
    assert "freight" in {q.id for q in questions.open_questions(n)}


def test_an_answer_given_only_in_words_is_recorded_on_the_follow_up(event, monkeypatch):
    seen: list[str] = []
    script = [
        [SimpleNamespace(type="text", text="Thank you. Line 2 has no price: not offering?")],
        [SimpleNamespace(type="tool_use", id="t1", name="record_answer",
                         input={"question_id": f"line-{event.lines[3]}", "value": "10",
                                "unit": "each"})],
        [SimpleNamespace(type="text", text="Saved. Line 2 has no price: not offering?")],
    ]
    monkeypatch.setattr(agent.llm, "complete", _fake_llm(script, seen))
    reply = agent.run_turn(event.north, "There are 10 vests in a box.")
    assert _cell(event.north, event.lines[3])["unit_price_inr"] == "145.0000"
    assert reply.startswith("Saved.") and "Saved: line 3: price is for 10 each." in reply
    assert agent.NUDGE in seen[1]


def test_a_message_that_answers_nothing_keeps_the_first_reply(event, monkeypatch):
    script = [
        [SimpleNamespace(type="text", text="I can only help with your own quote.")],
        [SimpleNamespace(type="text", text="(nothing to record)")],
    ]
    monkeypatch.setattr(agent.llm, "complete", _fake_llm(script, []))
    reply = agent.run_turn(event.north, "Am I the lowest?")
    assert reply == "I can only help with your own quote."


# --- UX audit, Group A: what the vendor reads, confirmations, new questions ----------------


def test_vendor_never_sees_internal_tokens_or_failure_text(event, monkeypatch):
    """B3: the model's text is checked in code; machinery never reaches the vendor."""
    for fmt in questions.ANSWER_FORMAT.values():
        assert "NO_BID" not in fmt and "INCLUDED" not in fmt and "'YES'" not in fmt
    script = [[SimpleNamespace(type="text", text="Please answer for each line, for example: "
                               "'Line 2: NO_BID'.")]]
    monkeypatch.setattr(agent.llm, "complete", _fake_llm(script, []))
    reply = agent.run_turn(event.north, "")
    assert "NO_BID" not in reply and reply.startswith("Thank you.")
    leaky = "Lines 12 and 20 did not save. The system wants a number."
    assert agent.vendor_safe(f"Thanks.\n\n{leaky}", event.north) == "Thanks."
    for bad in ("question_id line-3", "value null", "Error: no open question"):
        assert bad not in agent.vendor_safe(bad, event.north)


def test_tool_errors_are_hints_not_quotes_of_the_vendor(event):
    """B3: an error goes back to the model as a plain retry hint, never 'X does not appear'."""
    with pytest.raises(answers.AnswerError) as err:
        answers.record_answer(event.north, f"line-{event.lines[3]}", "box", "each", "one box")
    assert "Ask" in str(err.value) and "'box'" not in str(err.value)
    # "one" is a number the vendor may type in words
    out = answers.record_answer(event.north, f"line-{event.lines[3]}", "one", "each",
                                "one vest per box")
    assert out["value"] == "1"


def test_plain_fixed_answers_are_understood(event):
    n, lines = event.north, event.lines
    answers.record_answer(n, f"line-{lines[2]}", "not offered", None, "We do not offer it")
    assert _cell(n, lines[2])["state"] == "not_quoted"


def test_confirming_a_converted_line_makes_it_verified_by_vendor(event):
    """H1: a price the vendor confirms as shown on the summary is Verified (by vendor) in the
    buyer's grid, and the vendor page says so only when the grid does."""
    n, lines = event.north, event.lines
    answers.record_answer(n, f"line-{lines[3]}", "10", "each", "A box has 10 vests.")
    answers.record_answer(n, f"line-{lines[2]}", "not offered", None, "Not offering gloves.")
    answers.record_answer(n, f"line-{lines[4]}", "85", "pack", "Cable ties 85 per pack")
    ties = _cell(n, lines[4])  # per pack -> per each by the line's pack size: an assumption
    assert ties["state"] == "inferred" and ties["confirmed_by"] == "vendor"
    status = {r["line_no"]: r["status"] for r in questions.quote_summary(n)["lines"]}
    assert status[4] == "read"  # not yet confirmed as converted, so it can be confirmed
    assert answers.confirm(n, [lines[4]]) == 1
    assert _cell(n, lines[4])["state"] == "confirmed"
    assert questions.quote_summary(n)["lines"][3]["status"] == "confirmed by you"
    assert answers.last_confirmed(n) is not None


def test_a_read_note_quoting_an_unreadable_price_is_asked_once(event):
    """H2: a note about the same smudged figure joins the price question and closes with it."""
    n, lines = event.north, event.lines
    with db.begin() as conn:
        run = conn.execute(db.extraction_run.insert().values(stage="extract", status="ok")
                           ).inserted_primary_key[0]
        cid = conn.execute(db.quote_claim.insert().values(
            rfx_id=event.rfx, vendor_id=n.vendor_id, run_id=run, vendor_item_text="Helmet",
            raw_price="2?5", raw_currency="INR", basis={"per": "each", "multiple": None},
            scope="line", evidence_quote="Helmet 2?5 each", evidence_ok=True)
        ).inserted_primary_key[0]
        conn.execute(db.claim_line.insert().values(claim_id=cid, rfx_line_id=lines[1]))
        conn.execute(db.claim_line.insert().values(claim_id=cid, rfx_line_id=lines[3]))
        conn.execute(db.flag.insert().values(
            rfx_id=event.rfx, vendor_id=n.vendor_id, run_id=run, target_kind="claim",
            target_id=cid, type="read_mismatch", severity="warning", state="open",
            reason="two reads differ", candidates=["2?5", "245"]))
        note = conn.execute(db.flag.insert().values(
            rfx_id=event.rfx, vendor_id=n.vendor_id, run_id=run, target_kind="attachment",
            type="missing_info", severity="info", state="open",
            reason="unclear in the source: the rate's middle digit is smudged: 2?5.")
        ).inserted_primary_key[0]
    pipeline_run.post_checks(event.rfx)
    qs = questions.open_questions(n)
    price = next(q for q in qs if q.id == f"claim-{cid}")
    assert note in price.flag_ids and f"flag-{note}" not in {q.id for q in qs}
    answers.record_answer(n, price.id, "245", "each", "The helmet rate is 245 each.")
    assert _flag_state(note) == "closed"


def test_a_new_point_about_freight_opens_a_freight_question(event):
    """H2: "freight is extra at actuals for one plant" is kept and opens a freight question."""
    n = event.north
    assert "freight" not in {q.id for q in questions.open_questions(n)}
    out = agent.run_tool(n, "record_answer", {"question_id": "new_point",
                                              "value": "freight to the east plant is extra",
                                              "unit": None},
                         "Freight to the east plant is extra at actuals.", None)
    assert out["recorded"] == "new_point"
    assert "freight" in {q.id for q in questions.open_questions(n)}


def test_open_required_answers_are_asked_and_judged_by_code(event):
    """M12: a required question with no answer is asked; the answer is judged by the rule."""
    n = event.north
    reqs = [q for q in questions.open_questions(n) if q.kind == "requirement"]
    assert [q.id.startswith("req-") for q in reqs] == [True, True]
    lead = next(q for q in reqs if "dispatch" in q.text)
    assert not questions.blocks_confirm(lead)  # prices can be confirmed meanwhile
    answers.record_answer(n, lead.id, "7 days", None, "We dispatch in 7 days.")
    with db.connect() as conn:
        verdict = conn.execute(sa.select(db.answer.c.verdict).where(
            db.answer.c.vendor_id == n.vendor_id)).scalar()
    assert verdict == "pass"
    assert lead.id not in {q.id for q in questions.open_questions(n)}
