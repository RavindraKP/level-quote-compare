"""UX audit, Group B: Review, Compare, Analyst, Award, receipts, exports and emails.

One test (or more) per blocker B1, B4, B5, B6, B8, B9, then the High and Medium items that
can be checked without a browser. No model is called.
"""

import io
import json
import re
from decimal import Decimal as D
from pathlib import Path

import openpyxl
import pytest
import sqlalchemy as sa
from docx import Document

from level import copy, db
from level.agents.analyst import agent, audit, memo, steps, tools
from level.core import review as core_review
from level.core import scenario as core_scenario
from level.pipeline import clarify
from tests.test_analyst import event  # noqa: F401  (the small safety-supplies event)

ROOT = Path(__file__).resolve().parent.parent
RAW = re.compile(r"[{}\[\]]|Decimal\(|\bNone\b|question_id|vendor_id|_inr\b|USDINR|"
                 r"history\.|factor\.|\bNO_BID\b")


def _audit(conn, actor, action, kind, target, after):
    conn.execute(db.audit_event.insert().values(actor=actor, action=action, target_kind=kind,
                                                target_id=target, after=after))


def _flag(conn, rfx, vendor, line, type_="reference", reason="price taken from this vendor's "
          "own last record (\"same as last year\")"):
    return conn.execute(db.flag.insert().values(
        rfx_id=rfx, vendor_id=vendor, rfx_line_id=line, type=type_, severity="warning",
        reason=reason, state="open", target_kind="cell")).inserted_primary_key[0]


@pytest.fixture
def awarded(event):  # noqa: F811
    """The small event with a saved award option, a buyer edit and reply-link answers."""
    rfx, v, lines = event["rfx"], event["vendors"], event["lines"]
    ctx = tools.TurnContext(rfx_id=rfx)
    out = tools.run_scenario(ctx, {"strategy": "cheapest_per_line", "price_basis": "ex_works",
                                   "gating": "ignore",
                                   "fx": [{"currency": "USD", "rate": "90"}]})
    with db.begin() as conn:
        f1 = _flag(conn, rfx, v["North"], lines[1])
        f2 = _flag(conn, rfx, v["South"], lines[3])
        conn.execute(db.thread.insert().values(kind="vendor_link", rfx_id=rfx,
                                               vendor_id=v["South"]))
        _audit(conn, "buyer", "flag.edit_value", "flag", f1,
               {"action": "edit_value", "value": "3.28", "note": None})
        # B1: vendor answers on the reply link are words, not numbers
        for value in ("YES", "NO_BID", "The smudged 7-ply rate is 47.50 per kg"):
            _audit(conn, "vendor", "reply_link.answer", "question", None,
                   {"question_id": f"flag-{f2}", "kind": "text", "value": value,
                    "unit": None, "vendor_id": v["South"]})
        _audit(conn, "vendor", "reply_link.confirm", "vendor", v["South"],
               {"lines_confirmed": 3, "lines_asked": "all"})
        # another event's co-pilot edits must never appear in this event's trail (B8)
        other = conn.execute(db.rfx.insert().values(title="Another draft", status="draft")
                             ).inserted_primary_key[0]
        _audit(conn, "copilot", "rfx.lines.upsert", "rfx", other, {"line_nos": [31]})
        _audit(conn, "copilot", "rfx.import", "rfx", other, {"source_rfx_id": rfx})
    return {**event, "scenario": out["scenario_id"], "f1": f1, "f2": f2}


# --- B1: the memo builds whatever a vendor answered ----------------------------------------

def test_memo_builds_after_reply_link_answers_that_are_not_numbers(awarded):
    data = memo.award_memo(awarded["rfx"], awarded["scenario"])  # used to raise InvalidOperation
    doc = Document(io.BytesIO(data))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "price set to ₹3.28" in text
    assert "“The smudged 7-ply rate is 47.50 per kg”" in text and "“not offered”" in text
    assert "“YES”" in text  # a text answer is quoted, never parsed as a price
    appendix = text[text.index("Appendix"):]
    assert not RAW.search(appendix), RAW.findall(appendix)


# --- B8: the audit trail is this event's, in words -----------------------------------------

def test_audit_trail_is_filtered_by_event_and_has_no_raw_values(awarded):
    rows = audit.trail(awarded["rfx"])
    whats = [r["what"] for r in rows]
    assert "Added or changed lines" not in whats and "Copied a past event" not in whats
    assert "Vendor answered a question" in whats and "Vendor confirmed their prices" in whats
    for r in rows:
        assert not RAW.search(r["details"]), r
        assert r["who"] in ("You", "Co-pilot", "Checks", "South", "North")
    answer = next(r for r in rows if r["what"] == "Vendor answered a question")
    assert answer["who"] == "South" and "line 3" in answer["details"]


def test_a_bulk_decision_is_one_row(awarded):
    with db.begin() as conn:
        ids = [_flag(conn, awarded["rfx"], awarded["vendors"]["North"], awarded["lines"][n])
               for n in (2, 4)]
        for i in ids:
            _audit(conn, "buyer", "flag.confirm", "flag", i,
                   {"action": "confirm", "value": None, "note": None, "batch": 7})
    rows = [r for r in audit.trail(awarded["rfx"]) if r["what"].startswith("Accepted")]
    assert [r["what"] for r in rows] == ["Accepted 2 prices"]
    assert rows[0]["details"] == "North: lines 2 and 4"


# --- B6 / M11: receipt history and reasons in words ----------------------------------------

def test_receipt_history_is_this_line_only_and_in_words(awarded):
    rows = audit.for_line(awarded["rfx"], awarded["vendors"]["South"], awarded["lines"][3])
    assert rows and all(not RAW.search(r["details"]) for r in rows)
    assert not any("3.28" in r["details"] for r in rows)  # North's line 1 edit is not here


@pytest.mark.parametrize("raw, words", [
    ("uses history.FY25 = 42.12 INR per piece (contract price)",
     "Uses last year's price (FY25): ₹42.12 per piece"),
    ("uses factor.g->each = multiply by 454", "Uses conversion per gram to per piece"),
    ("uses fx.USDINR = 86.70, gst_rate = 0.18", "Uses USD to INR rate 86.70, GST rate 18.0%"),
    ("unit = no unit stated = 1 piece", "No unit stated; taken as one piece"),
])
def test_cell_reasons_never_show_assumption_keys(raw, words):
    assert copy.plain(raw).startswith(words)
    assert not RAW.search(copy.plain(raw))


# --- B5: no dict, no internal key, no option id ---------------------------------------------

def test_fx_what_if_is_written_in_words(awarded):
    _, result = tools.load_scenario(awarded["scenario"])
    fx = [c for c in result.caveats if c.startswith("USD to INR taken as ₹90.00 for this option")]
    assert len(fx) == 1 and "reference rate of ₹" in fx[0]
    # the option's own rate only: never also "converted at" the reference rate (QA P3-M6)
    assert not any(c.startswith("USD prices converted at") for c in result.caveats)
    assert all(not RAW.search(c) for c in result.caveats)
    name = tools.scenario_name(result.params, tools._vendors(awarded["rfx"]))
    assert "USD at ₹90.00" in name and "USDINR" not in name


def test_chart_axes_have_titles_in_words(event):  # noqa: F811
    ctx = tools.TurnContext(rfx_id=event["rfx"])
    out = tools.run_scenario(ctx, {"strategy": "cheapest_per_line", "price_basis": "ex_works",
                                   "gating": "ignore"})
    tools.show_chart(ctx, out["result_id"], "by_vendor", "bar", "vendor", "total_inr", None,
                     "Totals")
    enc = ctx.artefacts[-1]["spec"]["encoding"]
    assert enc["x"]["title"] == "Vendor" and enc["y"]["title"] == "Annual value (₹)"
    assert "Cr" in enc["y"]["axis"]["labelExpr"]


def test_prompt_forbids_option_ids_and_western_grouping():
    from level.agents.analyst import prompt
    assert "never by\n  its scenario_id" in prompt.PRINCIPLES
    assert "Never write million" in prompt.PRINCIPLES


# --- B4: "How this was worked out" in words ------------------------------------------------

def test_steps_are_plain_lines_never_sql_or_json():
    calls = [
        {"name": "query_data", "input": {"sql": "SELECT vendor FROM v_cells"},
         "output": {"row_count": 30}, "error": None},
        {"name": "run_scenario", "input": {"strategy": "capped_vendors", "max_vendors": 2},
         "output": {"name": "Cheapest per line, at most 2 vendors · delivered"}, "error": None},
        {"name": "draft_email", "input": {"vendor_id": 3, "purpose": "missing_lines",
                                          "line_nos": None}, "output": {"drafted": True},
         "error": None},
        {"name": "export", "input": {"kind": "scenario_xlsx", "scenario_id": 16},
         "output": {"file": "award-option.xlsx"}, "error": None},
        {"name": "query_data", "input": {"sql": "SELEC"}, "output": None, "error": "SQL error"},
    ]
    lines = [steps.describe(c, {3: "Pacific Board"}) for c in calls]
    assert lines[0] == "Looked up the quotes: 30 rows"
    assert lines[2] == "Drafted an email to Pacific Board"
    assert "refused" in lines[4]
    for line in lines:
        assert not re.search(r"SELECT|[{}]|scenario_id|null|\b16\b", line), line


def test_analyst_page_shows_no_code_blocks():
    page = (ROOT / "app/views/5_Analyst.py").read_text(encoding="utf-8")
    assert "st.code(" not in page and "json.dumps" not in page


# --- B9: evidence is a quote, not a code block ---------------------------------------------

def test_group_b_pages_never_render_code_blocks():
    for rel in ("app/components/source_viewer.py", "app/components/receipt.py",
                "app/views/3_Review.py", "app/views/4_Compare.py", "app/views/6_Award.py"):
        assert "st.code(" not in (ROOT / rel).read_text(encoding="utf-8"), rel


# --- H3, H7, M16: emails -------------------------------------------------------------------

def test_clarification_email_carries_only_this_vendors_link(awarded):
    from level.agents.vendor_copilot.link import link_url, verify_token
    rfx, v = awarded["rfx"], awarded["vendors"]
    d = clarify.draft(rfx, v["South"], use_model=False)
    assert link_url(rfx, v["South"]) in d.body
    token = re.search(r"\?t=(\S+)", d.body).group(1)
    assert verify_token(token).vendor_id == v["South"]
    assert d.body.index("answer these points online") < d.body.index("Regards")


def test_covering_email_note_is_not_asked_when_files_were_read(awarded):
    rfx, v = awarded["rfx"], awarded["vendors"]
    with db.begin() as conn:
        mid = conn.execute(db.message.insert().values(rfx_id=rfx, vendor_id=v["West"],
                                                      direction="in")).inserted_primary_key[0]
        att = conn.execute(db.attachment.insert().values(
            message_id=mid, filename="q.pdf", path="x", sha256="0")).inserted_primary_key[0]
        conn.execute(db.quote_claim.insert().values(
            rfx_id=rfx, vendor_id=v["West"], attachment_id=att, raw_price="1"))
        conn.execute(db.flag.insert().values(
            rfx_id=rfx, vendor_id=v["West"], type="missing_info", severity="info",
            reason="unclear in the source: only the covering email was provided in this file; "
                   "no prices are read from it", state="open", target_kind="message",
            target_id=mid))
    assert clarify.draft(rfx, v["West"], use_model=False) is None


def test_analyst_missing_lines_email_uses_the_shared_builder(event):  # noqa: F811
    ctx = tools.TurnContext(rfx_id=event["rfx"])
    out = tools.draft_email(ctx, event["vendors"]["South"], "missing_lines", None)
    body = out["body"]
    assert "answer these points online" in body and body.startswith("Dear ")
    assert "Line 2: Nitrile gloves" in body and "Is that right?" not in body


def test_fixed_question_never_adds_is_that_right_after_a_question():
    g = clarify.Group(key=("missing_info", "x"), kind=clarify.FlagType.MISSING_INFO, snippet="")
    text, _ = clarify._fixed_question(g, {"reason": "unclear in the source: which unit is "
                                                    "being priced?", "suggested_action": None})
    assert text == "Which unit is being priced?"


def test_totals_in_reasons_carry_the_rupee_sign_without_paise():
    text = copy.plain("lines add up to 3,42,05,850.00; stated total is 3,40,25,850.00")
    assert text == "Lines add up to ₹3,42,05,850; stated total is ₹3,40,25,850"


# --- H4: Excel files stored without an extension -------------------------------------------

def test_excel_source_without_extension_opens(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "app"))
    from components import source_render as sr
    wb = openpyxl.Workbook()
    wb.active.title = "Rates"
    wb.active["B2"] = "42.50"
    path = tmp_path / "upload-7"  # how uploads are stored: no extension
    wb.save(io.BytesIO())
    buf = io.BytesIO()
    wb.save(buf)
    path.write_bytes(buf.getvalue())
    assert sr.xlsx_sheet_names(path) == ["Rates"]
    title, df = sr.xlsx_sheet(path, "Rates")
    assert df.loc[2, "B"] == "42.50"


def test_pdf_search_never_marks_short_numbers():
    import sys
    sys.path.insert(0, str(ROOT / "app"))
    from components import source_render as sr
    assert sr._search_terms("Rate 1 per box") == ["Rate 1 per box"]
    assert "41.50" in sr._search_terms("Rate 41.50 per kg")


# --- H5: answers are worked out by the worker ----------------------------------------------

def test_answer_job_is_registered_and_idempotent(event, monkeypatch):  # noqa: F811
    from level import worker
    assert agent.JOB_KIND in worker._HANDLERS
    thread = agent.new_thread(event["rfx"])
    qid = agent.save_question(thread, "Where do we stand?")
    agent._save_message(thread, "assistant", {"text": "Answered already."})

    def no_model(*_a, **_k):
        raise AssertionError("an answered question must not be asked again")

    monkeypatch.setattr(agent, "ask", no_model)
    job_id = agent.queue_answer(event["rfx"], thread, qid)
    ctx = worker.JobContext(job_id=job_id, kind=agent.JOB_KIND,
                            target=json.dumps({"rfx_id": event["rfx"], "thread_id": thread,
                                               "question_id": qid}), attempt=1)
    agent._answer_job(ctx)
    assert agent.jobs_for_thread(thread)[qid].id == job_id


def test_ask_with_a_saved_question_does_not_save_it_twice(event, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    from level import llm
    seen = {}

    def fake_complete(convo, **_k):
        seen["convo"] = convo
        msg = SimpleNamespace(content=[], stop_reason="end_turn")
        return SimpleNamespace(message=msg, text="Three replies are in.")

    monkeypatch.setattr(llm, "complete", fake_complete)
    thread = agent.new_thread(event["rfx"])
    qid = agent.save_question(thread, "How many replies?")
    agent.ask(event["rfx"], "How many replies?", thread_id=thread, question_id=qid)
    roles = [m["role"] for m in agent.messages(thread)]
    assert roles == ["user", "assistant"]
    assert [m["content"] for m in seen["convo"]].count("How many replies?") == 1


# --- H9: a vendor's own figure is grounded -------------------------------------------------

def test_a_vendors_own_crore_figure_counts_as_grounded():
    from level.agents.analyst import grounding
    results = ["Shreeji: rebate of 2% if offtake exceeds Rs 3.5 Cr"]
    assert grounding.unmatched("A rebate applies above ₹3.5 Cr.", results) == []


# --- M1, M2, M3, M5 -------------------------------------------------------------------------

def test_western_grouping_is_regrouped_without_changing_digits():
    assert copy.indian_numbers("₹50,756,767 and 290,000 pieces in 2026") == \
        "₹5,07,56,767 and 2,90,000 pieces in 2026"


def test_caveats_split_into_the_few_that_matter_and_terms_noted():
    caveats = [f"Vendor {i} excluded: does not meet a requirement." for i in range(7)]
    caveats += ["Delivered price = price + freight; payment terms not counted.",
                f"{core_scenario.TERM_NOTE}Shreeji: Rates valid till 31.03.2026.",
                "Vendor 0 excluded: does not meet a requirement."]
    parts = copy.caveat_parts(caveats)
    assert len(parts["main"]) == copy.CAVEATS_SHOWN and len(parts["more"]) == 2
    assert parts["basis"] and parts["terms"] == ["Shreeji: Rates valid till 31.03.2026."]
    assert copy.TERM_NOTE == core_scenario.TERM_NOTE


def test_scenario_caveats_use_plural_forms_not_brackets(event):  # noqa: F811
    ctx = tools.TurnContext(rfx_id=event["rfx"])
    out = tools.run_scenario(ctx, {"strategy": "single_vendor", "single_vendor_id":
                                   event["vendors"]["South"], "price_basis": "ex_works",
                                   "gating": "ignore"})
    assert all("(s)" not in c for c in out["caveats"])


@pytest.mark.parametrize("raw, words", [
    ("If it clears gating, the award changes", "If it meets the requirements, the award"),
    ("Bharat fails gating", "Bharat does not meet the requirements"),
    ("quoted item differs (bursting_factor_bf: RFx 20, quoted 16)", "(bursting factor: RFx 20"),
])
def test_plain_swaps_whole_phrases(raw, words):
    assert words in copy.plain(raw)


def test_no_unstated_unit_in_review_wording():
    reason = "unit assumed: no unit stated = 1 piece"
    summary = copy.issue_summary(copy.FlagType.MISSING_INFO, reason, 4, D("2.19"))
    assert "unstated" not in summary and "per piece" in summary
    assert copy.accept_label(copy.FlagType.MISSING_INFO, D("2.19"), True,
                             reason=reason) == "Accept ₹2.19 per piece"


def test_assumption_values_read_as_percent_and_rupees():
    assert copy.assumption_value("gst_rate", "0.18") == "18.0%"
    assert copy.assumption_value("cost_of_capital", "0.10") == "10.0% a year"
    assert copy.assumption_value("fx.USDINR", "86.7") == "₹86.70 per USD"


# --- M9: a unit slip does not top the queue ------------------------------------------------

def test_outlier_stake_is_taken_at_the_median():
    assert core_review.review_stake(D(1000), D(328), D("89.6"), D("3.66")) == D(3660)
    assert core_review.review_stake(D(1000), D(4), D("1.1"), D("3.66")) == D(4000)


# --- M17: exports read like the screens ----------------------------------------------------

def test_award_workbook_has_words_and_rounded_figures(awarded):
    from level.agents.analyst import award
    wb = openpyxl.load_workbook(io.BytesIO(award.scenario_xlsx(awarded["rfx"],
                                                               awarded["scenario"])))
    summary = {r[0].value: r[1].value for r in wb["Summary"].iter_rows()}
    assert summary["How lines are given out"] == "Cheapest eligible price on each line"
    assert str(summary["Annual value"]).startswith("₹")
    assert all(re.fullmatch(r"-?\d+(\.\d)?%|—", str(summary["Saving %"])) for _ in [0])
    sens = [[c.value for c in r] for r in wb["Sensitivity"].iter_rows(min_row=2)]
    assert all(row[2] in ("Yes", "No") for row in sens)
    flat = " ".join(str(c.value) for ws in wb for r in ws.iter_rows() for c in r if c.value)
    assert "True" not in flat and "(s)" not in flat


def test_review_reads_issue_state_for_the_two_tab_notice(awarded):
    import sys
    sys.path.insert(0, str(ROOT / "app"))
    from components import review_data
    assert review_data.is_open(awarded["f2"])
    with db.begin() as conn:
        conn.execute(db.flag.update().where(db.flag.c.id == awarded["f2"]).values(
            state="closed"))
    assert not review_data.is_open(awarded["f2"])
    with db.connect() as conn:
        assert conn.execute(sa.select(db.flag.c.state).where(
            db.flag.c.id == awarded["f1"])).scalar() == "open"


def test_quantities_never_get_a_rupee_sign():
    text = copy.indian_numbers(copy.plain("slab 'Up to 1,00,000 nos/yr' matches annual qty 80000"))
    assert "₹" not in text and "80,000" in text
