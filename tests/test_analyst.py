"""Analyst (WP6) without the model: T9 grounding, read-only data, tools, memo, summary.

Uses the small non-packaging event from pipeline_helpers (safety supplies, each/pair), so
nothing here depends on the evaluation dataset.
"""

from decimal import Decimal as D

import pytest
import sqlalchemy as sa

from level import db
from level.agents.analyst import grounding, tools
from level.agents.analyst.agent import Answer, ask
from level.core.cells import rebuild_cells
from level.core.summary import compare, risk
from level.types import Allocation, CellState, ScenarioParams, ScenarioResult
from tests import pipeline_helpers as H

# --- T9 grounding -----------------------------------------------------------------------


def test_number_not_in_tool_results_is_caught():
    results = ['{"total_inr": "12345678.12", "savings_inr": "250000.00"}']
    assert grounding.unmatched("The total is ₹1,23,45,678.12.", results) == []
    assert grounding.unmatched("The total is ₹1.23 crore, saving 2.5 lakh.", results) == []
    assert grounding.unmatched("The total is ₹1.30 crore.", results) == ["1.30 crore"]
    assert grounding.unmatched("That is 47,000 cheaper.", results) == ["47,000"]


def test_grounding_scales_percent_and_exemptions():
    results = [{"gst_rate": "0.18", "pct": "3.59"}, "lines 3, 14 and 27 are open"]
    assert grounding.unmatched("GST of 18% applies; savings 3.6%.", results) == []
    assert grounding.unmatched("Lines 14 and 27 are open.", results) == []
    # small counts and numbers the buyer typed are not figures the model produced
    assert grounding.unmatched("Two vendors, 3 lines.", results) == []
    assert grounding.unmatched("At 90 rupees per dollar.", results, question="dollar at 90?") == []
    assert grounding.unmatched("At 91 rupees per dollar.", results, question="dollar at 90?") == [
        "91"]


def test_grounding_ignores_list_markers_and_codes():
    assert grounding.unmatched("1. FY25 price\n2. ISO 9001", ["9001"]) == []


# --- Data and tools ----------------------------------------------------------------------


@pytest.fixture
def event(tmp_db):
    ids = H.seed()
    rfx, v, lines = ids["rfx"], ids["vendors"], ids["lines"]
    prices = {"North": ["200", "4.00", "145", "0.85"], "South": ["190", None, "160", "0.80"]}
    with db.begin() as conn:
        for vendor, row in prices.items():
            conn.execute(db.invitation.update().where(
                db.invitation.c.vendor_id == v[vendor]).values(status="replied"))
            conn.execute(db.vendor_term.insert().values(
                rfx_id=rfx, vendor_id=v[vendor], type="freight", text="freight included"))
            for no, price in enumerate(row, start=1):
                if price is None:
                    continue
                cid = conn.execute(db.quote_claim.insert().values(
                    rfx_id=rfx, vendor_id=v[vendor], vendor_item_text=f"item {no}",
                    raw_price=price, raw_currency="INR",
                    basis={"per": "pair" if no == 2 else "each", "multiple": None},
                    scope="line", evidence_locator="email, line 1", evidence_quote=price,
                    evidence_ok=True)).inserted_primary_key[0]
                conn.execute(db.claim_line.insert().values(claim_id=cid,
                                                           rfx_line_id=lines[no]))
    rebuild_cells(rfx)
    return ids


def test_query_data_is_read_only_and_limited_to_views(event):
    ctx = tools.TurnContext(rfx_id=event["rfx"])
    out = tools.query_data(ctx, f"SELECT vendor, COUNT(*) AS n FROM v_cells WHERE rfx_id = "
                                f"{event['rfx']} AND state != 'not_quoted' GROUP BY vendor")
    assert {r["vendor"]: r["n"] for r in out["rows"]}["South"] == 3
    for bad in ["UPDATE cell SET unit_price_inr = '1'", "SELECT 1; DELETE FROM cell",
                "SELECT * FROM quote_claim", "SELECT body FROM message",
                "WITH x AS (SELECT 1) DELETE FROM cell"]:
        with pytest.raises(tools.ToolError):
            tools.query_data(ctx, bad)


def test_chat_tools_cannot_change_extracted_data(event):
    """Run every tool that could touch data; claims, cells, terms and flags are unchanged."""
    def facts():
        with db.connect() as conn:
            return [conn.execute(sa.select(t)).all() for t in
                    (db.quote_claim, db.claim_line, db.cell, db.vendor_term, db.flag, db.answer)]

    before = facts()
    ctx = tools.TurnContext(rfx_id=event["rfx"])
    out = tools.run_scenario(ctx, {"strategy": "cheapest_per_line", "price_basis": "ex_works",
                                   "gating": "ignore",
                                   "fx": [{"currency": "USD", "rate": "90"}]})
    tools.pin_scenario(ctx, out["scenario_id"], "Split")
    tools.show_table(ctx, out["result_id"], "allocation", "Allocation", None)
    tools.show_chart(ctx, out["result_id"], "by_vendor", "bar", "vendor", "total_inr", None, "T")
    tools.get_evidence(ctx, event["vendors"]["North"], 1)
    tools.draft_email(ctx, event["vendors"]["South"], "missing_lines", None)
    tools.export(ctx, "award_memo_docx", out["scenario_id"])
    assert facts() == before


def test_scenario_tool_and_changing_a_price_changes_the_answer(event):
    ctx = tools.TurnContext(rfx_id=event["rfx"])
    args = {"strategy": "cheapest_per_line", "price_basis": "ex_works",
            "gating": "ignore"}
    out = tools.run_scenario(ctx, args)
    # by hand: helmet 190*2000 + gloves 4*50000 + vest 145*3000 + ties 0.80*200000
    assert out["total_inr"] == "1175000.00"
    assert out["by_vendor"] and ctx.caveats
    with db.begin() as conn:  # the database changes (e.g. a buyer correction) ...
        conn.execute(db.quote_claim.update().where(
            db.quote_claim.c.raw_price == "145").values(raw_price="170"))
    rebuild_cells(event["rfx"])
    out2 = tools.run_scenario(tools.TurnContext(rfx_id=event["rfx"]), args)
    assert out2["total_inr"] == "1220000.00"  # ... and so does the answer (vest now 160 x 3000)


def test_missing_lines_email_names_lines_only(event):
    ctx = tools.TurnContext(rfx_id=event["rfx"])
    out = tools.draft_email(ctx, event["vendors"]["South"], "missing_lines", None)
    assert out["drafted"] and "Line 2" in out["body"]
    assert "North" not in out["body"] and "4.00" not in out["body"]  # nothing about rivals
    with db.connect() as conn:
        status = conn.execute(sa.select(db.message.c.status).where(
            db.message.c.id == out["outbox_message_id"])).scalar()
    assert status == "draft"


def test_compare_scenarios_differences_come_from_code(event):
    ctx = tools.TurnContext(rfx_id=event["rfx"])
    a = tools.run_scenario(ctx, {"strategy": "cheapest_per_line", "price_basis": "ex_works",
                                 "gating": "ignore"})
    b = tools.run_scenario(ctx, {"strategy": "single_vendor", "price_basis": "ex_works",
                                 "gating": "ignore",
                                 "single_vendor_id": event["vendors"]["North"]})
    rows = tools.compare_scenarios(ctx, [a["scenario_id"], b["scenario_id"]])["comparison"]
    # North alone: 200*2000 + 4*50000 + 145*3000 + 0.85*200000 = 1,205,000
    assert rows[1]["total_inr"] == "1205000.00"
    assert rows[1]["difference_from_first_inr"] == "30000.00"


# --- Core summary ------------------------------------------------------------------------


def test_risk_counts_unconfirmed_allocated_value():
    alloc = [Allocation(rfx_line_id=1, vendor_id=1, unit_price_inr=D(10), annual_qty=D(100),
                        line_total_inr=D(1000), cell_state=CellState.INFERRED),
             Allocation(rfx_line_id=2, vendor_id=2, unit_price_inr=D(5), annual_qty=D(100),
                        line_total_inr=D(500), cell_state=CellState.CONFIRMED),
             Allocation(rfx_line_id=3, vendor_id=None, unit_price_inr=None, annual_qty=D(10),
                        line_total_inr=None, cell_state=None)]
    res = ScenarioResult(params=ScenarioParams(strategy="cheapest_per_line"), allocation=alloc,
                         vendors_used=[1, 2], total_inr=D(1500), last_year_total_inr=None,
                         savings_inr=None, savings_pct=None, coverage_gaps=[3], caveats=[],
                         sensitivity=[])
    r = risk(res)
    assert r["unconfirmed_count"] == 1 and r["exposure_inr"] == D("100.0")
    assert compare([res, res])[1]["total_minus_first_inr"] == 0


# --- The agent without a model: missing key stops, never fakes -----------------------------


def test_ask_without_key_raises(event, monkeypatch):
    monkeypatch.delenv("LEVEL_ANTHROPIC_API_KEY", raising=False)
    from level.llm import MissingAPIKey

    with pytest.raises(MissingAPIKey):
        ask(event["rfx"], "What is the cheapest split?")
    assert Answer  # importable from a plain Python call


def test_follow_up_may_repeat_figures_from_earlier_turns(event):
    """Earlier turns are replayed as text; their tool results still ground repeated figures."""
    from level.agents.analyst import agent

    tid = agent.new_thread(event["rfx"])
    mid = agent._save_message(tid, "assistant", {"text": "Total is 1175000.00"})
    agent._save_trace(tid, mid, [{"name": "run_scenario", "input": {},
                                  "output": {"total_inr": "1175000.00"}, "latency_ms": 1}])
    sources = agent._earlier_outputs(tid)
    assert grounding.unmatched("As before, ₹11.75 lakh.", sources) == []
    assert grounding.unmatched("Now ₹12.75 lakh.", sources) == ["12.75 lakh"]
