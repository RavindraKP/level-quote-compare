"""Award extras: the vendor split, finding the award once it is off the page, and
clarification drafts only to the vendors an award option uses (no model)."""

from decimal import Decimal as D

from level.agents.analyst import award, tools
from level.core import summary
from level.types import Allocation, CellState, ScenarioParams, ScenarioResult
from tests.test_analyst import event  # noqa: F401  (the small two-vendor event)


def _alloc(line, vendor, total, state):
    return Allocation(rfx_line_id=line, vendor_id=vendor, unit_price_inr=None,
                      annual_qty=D(1), line_total_inr=total, cell_state=state)


def test_vendor_split_groups_by_vendor_and_shares_the_total():
    result = ScenarioResult(
        params=ScenarioParams(strategy="cheapest_per_line"),
        allocation=[_alloc(1, 7, D(300), CellState.CONFIRMED),
                    _alloc(2, 7, D(100), CellState.INFERRED),
                    _alloc(3, 9, D(600), CellState.NEEDS_REVIEW),
                    _alloc(4, None, None, None)],  # no price: left out, not zero
        vendors_used=[7, 9], total_inr=D(1000), last_year_total_inr=None, savings_inr=None,
        savings_pct=None, coverage_gaps=[4], caveats=[], sensitivity=[])
    rows = summary.vendor_split(result)
    assert [r["vendor_id"] for r in rows] == [9, 7]  # largest first
    assert rows[0] == {"vendor_id": 9, "lines": 1, "total_inr": D(600), "share_pct": D(60),
                       "unconfirmed_lines": 1}
    assert rows[1]["lines"] == 2 and rows[1]["total_inr"] == D(400)
    assert rows[1]["share_pct"] == D(40) and rows[1]["unconfirmed_lines"] == 1
    no_total = result.model_copy(update={"total_inr": None})
    assert all(r["share_pct"] is None for r in summary.vendor_split(no_total))


def test_award_is_found_after_it_is_taken_off_the_page(event):  # noqa: F811
    rfx = event["rfx"]
    out = tools.run_scenario(tools.TurnContext(rfx_id=rfx),
                             {"strategy": "cheapest_per_line", "price_basis": "ex_works",
                              "gating": "ignore"})
    award.award(rfx, out["scenario_id"])
    award.set_pinned(out["scenario_id"], False)
    assert award.awarded(rfx)["id"] == out["scenario_id"]


def test_award_drafts_questions_only_to_the_winning_vendors(event, monkeypatch):  # noqa: F811
    rfx, v = event["rfx"], event["vendors"]
    asked = []
    monkeypatch.setattr(award.clarify, "save_draft",
                        lambda rfx_id, vendor_id: asked.append(vendor_id) or vendor_id)
    out = tools.run_scenario(tools.TurnContext(rfx_id=rfx),
                             {"strategy": "single_vendor", "single_vendor_id": v["North"],
                              "price_basis": "ex_works", "gating": "ignore"})
    assert award.award(rfx, out["scenario_id"]) == [v["North"]]
    assert asked == [v["North"]]
