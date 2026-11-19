"""'How this was worked out', in words: one plain line per tool step (UX audit B4).

The raw tool input (SQL, JSON) stays in the tool_call table for the evals; the buyer sees
what was done, never how it was typed: "Looked up the quotes: 30 rows",
"Worked out an award option: Cheapest per line, delivered, at most 2 vendors".
"""

from __future__ import annotations

from level import copy

STEP_LABEL = {"query_data": "Looked up the quotes", "run_scenario": "Worked out an award option",
              "compare_scenarios": "Compared award options", "show_table": "Drew a table",
              "show_chart": "Drew a chart", "get_evidence": "Opened the vendor's document",
              "export": "Made a file", "draft_email": "Drafted an email",
              "pin_scenario": "Saved an award option for the Award page"}


def label(name: str) -> str:
    """The step's name while it runs: 'Worked out an award option'."""
    return STEP_LABEL.get(name, "Working")


def describe(call: dict, vendors: dict[int, str]) -> str:
    """One finished step in words. `call` is a tool_call row: name, input, output, error."""
    name, args = call["name"], call.get("input") or {}
    out = call.get("output") if isinstance(call.get("output"), dict) else {}
    head = label(name)
    if call.get("error"):
        return f"{head}: this step was refused, so the analyst tried another way"
    detail = ""
    if name == "query_data":
        detail = copy.plural(int(out.get("row_count") or 0), "row")
    elif name == "run_scenario":
        detail = copy.plain(out.get("name") or "")
    elif name == "compare_scenarios":
        detail = copy.plural(len(args.get("scenario_ids") or []), "option")
    elif name in ("show_table", "show_chart"):
        detail = args.get("title") or ""
    elif name == "get_evidence":
        who = out.get("vendor") or vendors.get(args.get("vendor_id"), "the vendor")
        head = f"Opened {copy.possessive(who)} document"
        detail = f"line {args.get('line_no')}" if args.get("line_no") else ""
    elif name == "export":
        detail = out.get("file") or ""
    elif name == "draft_email":
        who = vendors.get(args.get("vendor_id"), "the vendor")
        head = f"Drafted an email to {who}"
        detail = "" if out.get("drafted", True) else "nothing to ask them for that purpose"
    elif name == "pin_scenario":
        detail = copy.plain(out.get("name") or "")
    return f"{head}: {detail}" if detail else head
