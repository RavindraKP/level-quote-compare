"""Read what the system produced (SQLite) into plain dicts keyed the way ground truth is.

Vendors are matched to truth keys by name and lines by line number, so scoring never depends
on database ids.
"""

from __future__ import annotations

from decimal import Decimal

import sqlalchemy as sa

from level import db


def take(truth: dict) -> dict:
    """cells[(key, line_no)], flags[], terms[], claims[], verdicts[key][q_no], jobs[]."""
    names = {v["name"]: k for k, v in truth["vendors"].items()}
    with db.connect() as conn:
        rows = lambda table: [r._asdict() for r in conn.execute(sa.select(table))]  # noqa: E731
        vendors = {r["id"]: names.get(r["name"]) for r in rows(db.vendor)}
        line_no = {r["id"]: r["line_no"] for r in rows(db.rfx_line)}
        q_no = {r["id"]: r["q_no"] for r in rows(db.question)}
        gating = sorted(r["q_no"] for r in rows(db.question) if r["is_gating"])

        cells = {}
        for r in rows(db.cell):
            key = (vendors.get(r["vendor_id"]), line_no.get(r["rfx_line_id"]))
            cells[key] = {**r, "unit": _dec(r["unit_price_inr"]),
                          "landed": _dec(r["landed_price_inr"])}
        flags = [{**r, "vendor": vendors.get(r["vendor_id"]),
                  "line_no": line_no.get(r["rfx_line_id"])} for r in rows(db.flag)]
        terms = [{**r, "vendor": vendors.get(r["vendor_id"])} for r in rows(db.vendor_term)]
        claims = [{**r, "vendor": vendors.get(r["vendor_id"])} for r in rows(db.quote_claim)]
        verdicts: dict = {}
        for r in rows(db.answer):
            verdicts.setdefault(vendors.get(r["vendor_id"]), {})[q_no.get(r["question_id"])] = (
                r["verdict"])
        jobs = rows(db.job)
    return {"cells": cells, "flags": flags, "terms": terms, "claims": claims,
            "verdicts": verdicts, "gating_questions": gating, "jobs": jobs}


def overall_verdict(snap: dict, key: str) -> str:
    """Gating verdict from the stored per-question verdicts; a missing answer is unclear."""
    got = [snap["verdicts"].get(key, {}).get(n) for n in snap["gating_questions"]]
    if "fail" in got:
        return "fail"
    return "pass" if got and all(v == "pass" for v in got) else "unclear"


def _dec(value) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))
