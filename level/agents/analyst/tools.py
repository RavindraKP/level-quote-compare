"""The analyst's tools (PLAN §7). Each is a plain function over SQLite and level/core.

None of them can change extracted data: SQL runs on a read-only connection, scenarios are
computed in memory, and the only writes are to the analyst's own tables (scenario, outbox
drafts, exports). Tool results are kept per turn under a result id ("r1", "r2", ...), so
tables and charts are drawn from tool data, never from numbers the model typed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

from level import copy, db
from level.core import export as export_mod
from level.core import summary
from level.core.cells import load_snapshot
from level.core.scenario import run_scenario as core_run_scenario
from level.pipeline import clarify
from level.types import ScenarioParams, ScenarioResult

ROW_CAP = 200
QUERY_SECONDS = 3.0
MONEY_DP = Decimal("0.01")


class ToolError(ValueError):
    """A problem the model can fix (bad SQL, unknown vendor). Sent back as an error result."""


@dataclass
class TurnContext:
    """What one analyst turn has produced so far."""

    rfx_id: int
    results: dict[str, dict] = field(default_factory=dict)  # result id -> {table: rows}
    artefacts: list[dict] = field(default_factory=list)  # tables, charts, files to show
    scenario_ids: list[int] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def store(self, tables: dict[str, list[dict]]) -> str:
        rid = f"r{len(self.results) + 1}"
        self.results[rid] = tables
        return rid


# --- Formatting --------------------------------------------------------------------------


def money(value) -> str | None:
    """Rupees to 2 decimals as a string (exact; no float)."""
    return None if value is None else str(Decimal(value).quantize(MONEY_DP))


def jsonable(x):
    """Decimals to strings, recursively, so tool output is plain JSON."""
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [jsonable(v) for v in x]
    return x


# --- Lookups -----------------------------------------------------------------------------


def _vendors(rfx_id: int) -> dict[int, str]:
    with db.connect() as conn:
        rows = conn.execute(sa.select(db.vendor.c.id, db.vendor.c.name).join(
            db.invitation, db.invitation.c.vendor_id == db.vendor.c.id).where(
            db.invitation.c.rfx_id == rfx_id)).all()
    return {r.id: r.name for r in rows}


def _lines(rfx_id: int) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sa.select(db.rfx_line).where(
            db.rfx_line.c.rfx_id == rfx_id).order_by(db.rfx_line.c.line_no)).mappings()]


def _vendor_id(rfx_id: int, vendor_id: int) -> int:
    if vendor_id not in _vendors(rfx_id):
        raise ToolError(f"vendor_id {vendor_id} is not invited to this RFx")
    return vendor_id


def _line_ids(rfx_id: int, line_nos: list[int]) -> list[int]:
    by_no = {ln["line_no"]: ln["id"] for ln in _lines(rfx_id)}
    missing = [n for n in line_nos if n not in by_no]
    if missing:
        raise ToolError(f"no such line numbers: {missing}")
    return [by_no[n] for n in line_nos]


# --- query_data --------------------------------------------------------------------------

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
# The tables the v_* views are built from. SQLite flattens a view into the query, so its
# authorizer sees these base tables rather than the view; anything else (vendor documents,
# claims, chat, audit log) cannot be read at all.
VIEW_TABLES = {"rfx_line", "vendor", "invitation", "cell", "vendor_term", "question",
               "answer", "flag", "price_history"}


def _authorizer(action, table, _column, _db, _view):
    """Allow plain reads of the documented views (and the tables behind them) only."""
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and (table in VIEW_TABLES or (table or "").startswith("v_")):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def readonly_select(sql: str) -> tuple[list[str], list[dict], bool]:
    """Run one SELECT on the read-only connection. Returns (columns, rows, truncated)."""
    sql = sql.strip().rstrip(";").strip()
    if ";" in sql:
        raise ToolError("one statement only")
    if not re.match(r"(?is)^(select|with)\b", sql):
        raise ToolError("only SELECT (or WITH ... SELECT) is allowed")
    deadline = time.monotonic() + QUERY_SECONDS
    with db.connect_readonly() as conn:
        raw = conn.connection.driver_connection
        raw.set_authorizer(_authorizer)
        raw.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        try:
            cur = raw.execute(sql)
            columns = [d[0] for d in cur.description or []]
            fetched = cur.fetchmany(ROW_CAP + 1)
        except sqlite3.DatabaseError as exc:
            raise ToolError(f"SQL error: {exc}") from exc
    rows = [dict(zip(columns, r, strict=True)) for r in fetched[:ROW_CAP]]
    return columns, rows, len(fetched) > ROW_CAP


def query_data(ctx: TurnContext, sql: str) -> dict:
    columns, rows, truncated = readonly_select(sql)
    rid = ctx.store({"rows": rows})
    return {"result_id": rid, "columns": columns, "rows": rows, "row_count": len(rows),
            "truncated": truncated}


# --- Scenarios ---------------------------------------------------------------------------


def scenario_name(p: ScenarioParams, vendors: dict[int, str]) -> str:
    """A plain label built from the parameters (shown on the Award page)."""
    if p.strategy == "single_vendor":
        who = vendors.get(p.single_vendor_id, "best single vendor")
        head = f"Single vendor: {who}"
    elif p.strategy == "capped_vendors":
        head = f"Cheapest per line, at most {p.max_vendors or 2} vendors"
    else:
        head = "Cheapest per line"
    parts = [head, "delivered" if p.price_basis == "landed" else "ex-works"]
    if p.price_basis == "landed" and p.include_payment_terms:
        parts.append("with payment terms")
    parts.append("requirements ignored" if not p.require_gating_pass else
                 "only vendors that meet every requirement" if p.exclude_unclear_gating
                 else "vendors that do not meet a requirement left out")
    if p.vendor_ids is not None:
        parts.append("only " + ", ".join(vendors.get(v, str(v)) for v in p.vendor_ids))
    if p.fx_override:  # "USD at ₹90.00", never "USDINR at 90" (UX audit B5)
        parts.append(", ".join(f"{k[:3]} at {copy.unit_price(Decimal(str(v)))}"
                               if k.endswith("INR") else f"{k[:3]} at {v} {k[3:]}"
                               for k, v in p.fx_override.items()))
    if p.apply_conditional_discounts:
        parts.append("conditional discounts applied")
    if p.excluded_line_ids:
        parts.append(f"{copy.plural(len(p.excluded_line_ids), 'line')} left out")
    if p.unconfirmed != "include":
        parts.append({"exclude": "unverified prices left out",
                      "pessimistic": "unverified prices raised 10%"}.get(p.unconfirmed,
                                                                         p.unconfirmed))
    return " · ".join(parts)


def params_from_args(rfx_id: int, args: dict) -> ScenarioParams:
    """Tool arguments (vendor ids, line numbers) -> ScenarioParams (vendor and line ids)."""
    vendor_ids = args.get("vendor_ids")
    for v in vendor_ids or []:
        _vendor_id(rfx_id, v)
    single = args.get("single_vendor_id")
    if single is not None:
        _vendor_id(rfx_id, single)
    fx = {f"{r['currency'].upper()}INR": Decimal(r["rate"]) for r in args.get("fx") or []}
    return ScenarioParams(
        strategy=args["strategy"], single_vendor_id=single, vendor_ids=vendor_ids,
        require_gating_pass=args.get("gating", "passed_only") != "ignore",
        exclude_unclear_gating=args.get("gating", "passed_only") == "passed_only",
        max_vendors=args.get("max_vendors"),
        excluded_line_ids=_line_ids(rfx_id, args.get("excluded_line_nos") or []),
        price_basis=args.get("price_basis") or "landed",
        include_payment_terms=bool(args.get("include_payment_terms")),
        fx_override=fx,
        apply_conditional_discounts=bool(args.get("apply_conditional_discounts")),
        unconfirmed=args.get("unconfirmed") or "include",
    )


def save_scenario(rfx_id: int, params: ScenarioParams, result: ScenarioResult) -> int:
    name = scenario_name(params, _vendors(rfx_id))
    with db.begin() as conn:
        return conn.execute(db.scenario.insert().values(
            rfx_id=rfx_id, name=name, params=params.model_dump(mode="json"),
            result=result.model_dump(mode="json"), pinned=False,
        )).inserted_primary_key[0]


def load_scenario(scenario_id: int) -> tuple[dict, ScenarioResult]:
    with db.connect() as conn:
        row = conn.execute(sa.select(db.scenario).where(db.scenario.c.id == scenario_id)
                           ).mappings().first()
    if row is None:
        raise ToolError(f"no scenario {scenario_id}")
    return dict(row), ScenarioResult.model_validate(row["result"])


def scenario_view(rfx_id: int, scenario_id: int, result: ScenarioResult) -> dict:
    """The scenario as the model (and tables) see it: names and line numbers, not ids."""
    vendors = _vendors(rfx_id)
    lines = {ln["id"]: ln for ln in _lines(rfx_id)}
    risk = summary.risk(result)
    allocation = [{
        "line_no": lines[a.rfx_line_id]["line_no"],
        "description": lines[a.rfx_line_id]["description"],
        "vendor": vendors.get(a.vendor_id) if a.vendor_id else None,
        "unit_price_inr": money(a.unit_price_inr), "annual_qty": str(a.annual_qty),
        "line_total_inr": money(a.line_total_inr),
        "state": a.cell_state.value if a.cell_state else "no eligible price",
    } for a in result.allocation]
    by_vendor = [{"vendor": vendors[v], "lines_won": s["lines"], "total_inr": money(s["total_inr"]),
                  "unconfirmed_lines": s["unconfirmed_lines"]}
                 for v, s in summary.by_vendor(result).items()]
    return {
        "scenario_id": scenario_id,
        "name": scenario_name(result.params, vendors),
        "total_inr": money(result.total_inr),
        "last_year_total_inr_same_lines": money(result.last_year_total_inr),
        "savings_vs_last_year_inr": money(result.savings_inr),
        "savings_vs_last_year_pct": money(result.savings_pct),
        "vendors_used": [vendors[v] for v in result.vendors_used],
        "by_vendor": by_vendor,
        "lines_without_eligible_price": [lines[i]["line_no"] for i in result.coverage_gaps],
        "risk": {
            "unconfirmed_prices_relied_on": risk["unconfirmed_count"],
            "unconfirmed_value_inr": money(risk["unconfirmed_value_inr"]),
            "exposure_if_10pct_off_inr": money(risk["exposure_inr"]),
            "lines_where_10pct_move_changes_winner":
                [lines[i]["line_no"] for i in risk["lines_winner_changes"]],
        },
        "caveats": result.caveats,
        "allocation": allocation,
    }


def run_scenario(ctx: TurnContext, args: dict) -> dict:
    params = params_from_args(ctx.rfx_id, args)
    result = core_run_scenario(ctx.rfx_id, params)
    sid = save_scenario(ctx.rfx_id, params, result)
    ctx.scenario_ids.append(sid)
    # One caveat per kind: a later option's "12 prices not yet verified" replaces an earlier
    # option's "15 prices ..." instead of both being listed (UX audit M2).
    ctx.caveats = merge_caveats(ctx.caveats, result.caveats)
    view = scenario_view(ctx.rfx_id, sid, result)
    rid = ctx.store({"allocation": view["allocation"], "by_vendor": view["by_vendor"]})
    return {"result_id": rid, "tables": ["allocation", "by_vendor"], **view}


_FX_CAVEAT = re.compile(r"^([A-Z]{3}) (?:prices converted at|to [A-Z]{3} taken as) ")


def _shape(caveat: str) -> str:
    """The kind of a caveat: its numbers, line lists and plurals blanked ('12 prices ...:
    lines 1, 3 and 6' and '1 price ...: line 4' are one kind). Every exchange-rate caveat for
    one currency is one kind, so a turn never shows two rates for it."""
    fx = _FX_CAVEAT.match(caveat)
    if fx:
        return f"fx:{fx.group(1)}"
    shape = re.sub(r"\d[\d,.]*", "#", caveat.rstrip("."))
    shape = re.sub(r"#(?:\s*(?:,|and|to|[-–])?\s*#)+", "#", shape)  # a list of numbers
    shape = re.sub(r"\blines #", "line #", shape)
    return re.sub(r"# (\w+?)s\b", r"# \1", shape)


def _is_what_if_rate(caveat: str) -> bool:
    return bool(_FX_CAVEAT.match(caveat)) and " taken as " in caveat


def merge_caveats(earlier: list[str], newer: list[str]) -> list[str]:
    """One caveat per kind across the options run in one turn: the newer option's replaces
    the earlier one's, except that a rate set for an option (a what-if) is kept over the
    reference rate, so the caveats match the option the buyer asked about."""
    out = list(earlier)
    for c in newer:
        kind = _shape(c)
        same = [o for o in out if _shape(o) == kind]
        if any(_is_what_if_rate(o) for o in same) and not _is_what_if_rate(c):
            continue
        out = [o for o in out if _shape(o) != kind] + [c]
    return out


def compare_scenarios(ctx: TurnContext, scenario_ids: list[int]) -> dict:
    """Headline figures side by side and each one's difference from the first."""
    if not scenario_ids:
        raise ToolError("give at least one scenario_id")
    vendors = _vendors(ctx.rfx_id)
    loaded = [load_scenario(s) for s in scenario_ids]
    rows = []
    for (row, res), h in zip(loaded, summary.compare([r for _, r in loaded]), strict=True):
        rows.append({"scenario_id": row["id"], "name": scenario_name(res.params, vendors),
                     "total_inr": money(h["total_inr"]),
                     "note": (None if h["total_inr"] is not None else
                              "no total: no line has a price on this basis"),
                     "difference_from_first_inr": money(h["total_minus_first_inr"]),
                     "difference_from_first_pct": money(h["pct_vs_first"]),
                     "savings_vs_last_year_inr": money(h["savings_inr"]),
                     "vendors_used": h["vendors_used"],
                     "vendor_names": [vendors[v] for v in res.vendors_used],
                     "lines_without_quote": h["lines_without_quote"],
                     "unconfirmed_prices_relied_on": h["unconfirmed_count"],
                     "exposure_if_10pct_off_inr": money(h["exposure_inr"])})
    rid = ctx.store({"comparison": rows})
    return {"result_id": rid, "tables": ["comparison"], "comparison": rows}


def pin_scenario(ctx: TurnContext, scenario_id: int, name: str | None) -> dict:
    row, _ = load_scenario(scenario_id)
    if row["rfx_id"] != ctx.rfx_id:
        raise ToolError("that scenario belongs to another RFx")
    values = {"pinned": True}
    if name:
        values["name"] = name.strip()[:120]
    with db.begin() as conn:
        conn.execute(db.scenario.update().where(db.scenario.c.id == scenario_id).values(**values))
    return {"pinned": scenario_id, "name": values.get("name", row["name"])}


# --- Tables and charts -------------------------------------------------------------------


def _table(ctx: TurnContext, result_id: str, table: str | None) -> list[dict]:
    tables = ctx.results.get(result_id)
    if tables is None:
        raise ToolError(f"no result {result_id} in this turn; call a data tool first")
    name = table or next(iter(tables))
    if name not in tables:
        raise ToolError(f"result {result_id} has tables {list(tables)}, not {name}")
    return tables[name]


def show_table(ctx: TurnContext, result_id: str, table: str | None, title: str,
               columns: list[str] | None) -> dict:
    rows = _table(ctx, result_id, table)
    if columns:
        unknown = [c for c in columns if rows and c not in rows[0]]
        if unknown:
            raise ToolError(f"unknown columns {unknown}")
        rows = [{c: r.get(c) for c in columns} for r in rows]
    ctx.artefacts.append({"kind": "table", "title": title, "rows": rows})
    return {"shown": "table", "rows": len(rows)}


# Chart tick labels in crore and lakh (display only; the data values are unchanged).
INR_TICKS = ("datum.value >= 10000000 ? '₹' + format(datum.value / 10000000, '.2f') + ' Cr' : "
             "datum.value >= 100000 ? '₹' + format(datum.value / 100000, '.1f') + ' L' : "
             "'₹' + format(datum.value, ',.0f')")


def _number_or_text(v):
    """Chart data needs real numbers; money arrives as decimal strings."""
    if isinstance(v, str):
        try:
            return float(Decimal(v))
        except ArithmeticError:
            return v
    return v


def show_chart(ctx: TurnContext, result_id: str, table: str | None, mark: str, x: str, y: str,
               color: str | None, title: str) -> dict:
    rows = _table(ctx, result_id, table)
    fields = [f for f in (x, y, color) if f]
    unknown = [f for f in fields if rows and f not in rows[0]]
    if unknown:
        raise ToolError(f"unknown fields {unknown}; columns are {list(rows[0]) if rows else []}")
    values = [{f: _number_or_text(r.get(f)) for f in fields} for r in rows]
    # A missing value is unknown, not zero: leave that row out of the chart and say so.
    left_out = [v[x] for v in values if v[y] is None]
    values = [v for v in values if v[y] is not None]

    def kind(f):
        return ("quantitative" if values and all(isinstance(v[f], int | float) for v in values
                                                 if v[f] is not None) else "nominal")

    def axis(f):  # a heading in words and rupees as Cr / L, never 'total_inr' (UX audit B5)
        out = {"field": f, "type": kind(f), "title": copy.column_label(f)}
        if kind(f) == "quantitative" and f.endswith("_inr"):
            out["axis"] = {"labelExpr": INR_TICKS}
        return out

    encoding = {"x": axis(x), "y": axis(y)}
    if mark == "arc":
        encoding = {"theta": {"field": y, "type": "quantitative", "title": copy.column_label(y)},
                    "color": {"field": x, "type": "nominal", "title": copy.column_label(x)}}
    elif color:
        encoding["color"] = {"field": color, "type": "nominal", "title": copy.column_label(color)}
    spec = {"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "title": title,
            "data": {"values": values}, "mark": {"type": mark, "tooltip": True},
            "encoding": encoding}
    ctx.artefacts.append({"kind": "chart", "title": title, "spec": spec})
    out = {"shown": "chart", "points": len(values)}
    if left_out:
        out["left_out_no_value"] = left_out
    return out


# --- Evidence, exports, emails -----------------------------------------------------------


def get_evidence(ctx: TurnContext, vendor_id: int, line_no: int) -> dict:
    """The receipt for one cell: as quoted, the vendor's words, conversion, state, flags."""
    _vendor_id(ctx.rfx_id, vendor_id)
    line_id = _line_ids(ctx.rfx_id, [line_no])[0]
    with db.connect() as conn:
        cell = conn.execute(sa.select(db.cell).where(
            db.cell.c.vendor_id == vendor_id, db.cell.c.rfx_line_id == line_id)).mappings().first()
        if cell is None:
            raise ToolError("no cell yet for that vendor and line")
        claim = conn.execute(sa.select(db.quote_claim).where(
            db.quote_claim.c.id == cell["claim_id"])).mappings().first()
        match = conn.execute(sa.select(db.claim_line).where(
            db.claim_line.c.claim_id == cell["claim_id"],
            db.claim_line.c.rfx_line_id == line_id)).mappings().first()
        flags = conn.execute(sa.select(db.flag).where(
            db.flag.c.id.in_(cell["flag_ids"] or []))).mappings().all()
        filename = None
        if claim and claim["attachment_id"]:
            filename = conn.execute(sa.select(db.attachment.c.filename).where(
                db.attachment.c.id == claim["attachment_id"])).scalar()
    out = {
        "vendor": _vendors(ctx.rfx_id)[vendor_id], "line_no": line_no, "state": cell["state"],
        "reason": cell["reason"], "as_quoted": cell["as_quoted"],
        "unit_price_inr": cell["unit_price_inr"], "landed_price_inr": cell["landed_price_inr"],
        "conversion_steps": cell["conversion_steps"], "assumptions": cell["assumptions"],
        "source": None, "match": None,
        "flags": [{"type": f["type"], "state": f["state"], "reason": f["reason"]} for f in flags],
    }
    if claim:
        out["source"] = {"file": filename or "email body", "locator": claim["evidence_locator"],
                         "quote": claim["evidence_quote"], "item_text": claim["vendor_item_text"],
                         "evidence_check_passed": claim["evidence_ok"]}
    if match:
        out["match"] = {"rationale": match["match_rationale"],
                        "spec_deviation": match["spec_deviation"]}
    ctx.store({"evidence": [out]})
    return out


def exports_dir() -> Path:
    path = db.db_path().parent / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def export(ctx: TurnContext, kind: str, scenario_id: int | None) -> dict:
    from level.agents.analyst import memo  # local: python-docx only when needed

    stamp = time.strftime("%Y%m%d-%H%M%S")
    if kind == "comparison_xlsx":
        data, name = export_mod.comparison_xlsx(load_snapshot(ctx.rfx_id)), "comparison"
        suffix = "xlsx"
    else:
        if scenario_id is None:
            raise ToolError(f"{kind} needs a scenario_id")
        _, result = load_scenario(scenario_id)
        if kind == "scenario_xlsx":
            data = export_mod.scenario_xlsx(result, load_snapshot(ctx.rfx_id))
            name, suffix = "award-option", "xlsx"
        elif kind == "award_memo_docx":
            data = memo.award_memo(ctx.rfx_id, scenario_id)
            name, suffix = "award-memo", "docx"
        else:
            raise ToolError(f"unknown export kind {kind}")
    path = exports_dir() / f"{name}-{stamp}.{suffix}"
    path.write_bytes(data)
    ctx.artefacts.append({"kind": "file", "title": path.name, "path": str(path)})
    return {"file": path.name}


def draft_email(ctx: TurnContext, vendor_id: int, purpose: str,
                line_nos: list[int] | None) -> dict:
    """Write a draft to the outbox. Wording is fixed by code; nothing is ever sent."""
    _vendor_id(ctx.rfx_id, vendor_id)
    # One email builder for every path: same shape, date, signature and reply link (M16).
    if purpose == "missing_lines":
        d = clarify.missing_lines_draft(ctx.rfx_id, vendor_id, line_nos)
    elif purpose == "clarification":
        d = clarify.draft(ctx.rfx_id, vendor_id)
    else:
        raise ToolError("purpose is missing_lines or clarification")
    body, subject = (d.body, d.subject) if d else (None, "")
    if body is None:
        return {"drafted": False, "reason": "nothing to ask this vendor for that purpose"}
    with db.begin() as conn:
        msg_id = conn.execute(db.message.insert().values(
            rfx_id=ctx.rfx_id, vendor_id=vendor_id, direction="out", channel="email",
            status="draft", subject=subject, body=body)).inserted_primary_key[0]
    ctx.artefacts.append({"kind": "email", "title": subject, "body": body,
                          "message_id": msg_id})
    return {"drafted": True, "outbox_message_id": msg_id, "subject": subject, "body": body,
            "note": "Saved as a draft in the outbox. The buyer sends it; you cannot."}


def run(ctx: TurnContext, name: str, args: dict) -> dict:
    """Dispatch one tool call."""
    if name == "query_data":
        return query_data(ctx, args["sql"])
    if name == "run_scenario":
        return run_scenario(ctx, args)
    if name == "compare_scenarios":
        return compare_scenarios(ctx, args["scenario_ids"])
    if name == "pin_scenario":
        return pin_scenario(ctx, args["scenario_id"], args.get("name"))
    if name == "show_table":
        return show_table(ctx, args["result_id"], args.get("table"), args["title"],
                          args.get("columns"))
    if name == "show_chart":
        return show_chart(ctx, args["result_id"], args.get("table"), args["mark"], args["x"],
                          args["y"], args.get("color"), args["title"])
    if name == "get_evidence":
        return get_evidence(ctx, args["vendor_id"], args["line_no"])
    if name == "export":
        return export(ctx, args["kind"], args.get("scenario_id"))
    if name == "draft_email":
        return draft_email(ctx, args["vendor_id"], args["purpose"], args.get("line_nos"))
    raise ToolError(f"unknown tool {name}")


def to_text(output: dict) -> str:
    return json.dumps(jsonable(output), ensure_ascii=False)
