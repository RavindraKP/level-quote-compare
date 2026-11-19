"""Stage 2: match vendor rows to RFx lines (model), then check the mapping (code).

The model proposes; code then drops line numbers that do not exist, keeps a reference off
lines another row prices (line price > rule > reference), expands a "the rest" reference to
every line no other row covers, and compares dimension-like attributes it can
read from the row text with the line's spec (T5).
"""

from __future__ import annotations

import json
import re

from level import llm
from level.pipeline import prompts
from level.types import ExtractedRow, MatchOutput, RowMatch, SpecDeviation

_DIMS = re.compile(r"(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)(?:\s*[x×*]\s*(\d+(?:\.\d+)?))?",
                   re.IGNORECASE)


def lines_text(lines: list[dict]) -> str:
    out = []
    for ln in lines:
        spec = json.dumps(ln.get("spec") or {}, ensure_ascii=False, sort_keys=True)
        out.append(f"line {ln['line_no']}: {ln['description']} | unit: {ln['canonical_uom']} "
                   f"| spec: {spec}")
    return "\n".join(out)


def rows_text(rows: list[ExtractedRow]) -> str:
    return "\n".join(
        f"{i}: {r.item_text!r} price={r.price!r} unit={r.uom_text!r}"
        + (f" slab={r.quantity_break.text!r}" if r.quantity_break else "")
        for i, r in enumerate(rows))


def match(rows: list[ExtractedRow], lines: list[dict]) -> tuple[MatchOutput, llm.LLMResult]:
    content = (
        "RFx lines (buyer's data, trusted):\n" + lines_text(lines) + "\n\nVendor rows:\n"
        + llm.untrusted("vendor rows", rows_text(rows))
        + "\n\nReturn one match per row_index."
    )
    result = llm.structured(MatchOutput, [{"role": "user", "content": content}],
                            system=prompts.MATCH_SYSTEM, effort="medium", max_tokens=32000)
    return result.parsed, result


def dimension_deviations(item_text: str, spec: dict) -> list[SpecDeviation]:
    """Code check: a size written in the row (AxBxC) must equal the line's size attribute."""
    found = _DIMS.search(item_text or "")
    if not found:
        return []
    quoted = [n for n in found.groups() if n]
    out = []
    for key, value in (spec or {}).items():
        m = _DIMS.search(str(value))
        if not m:
            continue
        expected = [n for n in m.groups() if n]
        if len(expected) == len(quoted) and [float(x) for x in expected] != [
                float(x) for x in quoted]:
            out.append(SpecDeviation(attribute=str(key), rfx_value=str(value),
                                     quoted_value=found.group(0)))
    return out


def checked_matches(output: MatchOutput, rows: list[ExtractedRow], lines: list[dict]
                    ) -> list[RowMatch]:
    """Clean the model's mapping with code rules. Returns one RowMatch per row."""
    valid = {ln["line_no"] for ln in lines}
    spec_by_no = {ln["line_no"]: ln.get("spec") or {} for ln in lines}
    by_row = {m.row_index: m for m in output.matches if 0 <= m.row_index < len(rows)}
    result = []
    for i in range(len(rows)):
        m = by_row.get(i) or RowMatch(row_index=i, scope="none", rfx_line_nos=[],
                                      rule_text=None, rationale="no match returned",
                                      spec_deviations=[])
        m.rfx_line_nos = sorted({n for n in m.rfx_line_nos if n in valid})
        if m.scope == "line" and len(m.rfx_line_nos) == 1:
            extra = dimension_deviations(rows[i].item_text, spec_by_no[m.rfx_line_nos[0]])
            known = {d.attribute for d in m.spec_deviations}
            m.spec_deviations += [d for d in extra if d.attribute not in known]
        result.append(m)
    # Precedence within one reply: an explicit line price, then a rule, then a reference to
    # last year. So a reference never takes a line another row prices, and a reference with
    # no lines listed means "the rest": every line no other row prices.
    covered = {n for m in result if m.scope in ("line", "rule") for n in m.rfx_line_nos}
    for m in result:
        if m.scope == "reference":
            m.rfx_line_nos = sorted((set(m.rfx_line_nos) or valid) - covered)
    return result
