"""T9: every number in the analyst's answer must come from a tool result of this turn.

How it works:
1. Pull every number out of the answer text, with its scale (lakh, crore, million, k, %)
   and how many decimals were written.
2. Pull every number out of the tool results (JSON values and numbers inside strings).
3. A written number is grounded if some tool number, divided by the written scale and
   rounded to the written decimals, equals it. So "Rs 1.23 crore" matches 12,345,678 and
   "12%" matches 12.04. A share like 0.12 also matches "12%".
4. Small whole numbers (0-10) and numbers the buyer typed in the question are exempt: they
   are counts and references ("three suppliers", "line 7"), not figures the model produced.

The answer is not trusted if anything is left over; the agent retries once, naming the
unmatched numbers, and otherwise shows the answer with a visible warning.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

SCALES = {"crore": 10**7, "crores": 10**7, "cr": 10**7, "lakh": 10**5, "lakhs": 10**5,
          "lac": 10**5, "lacs": 10**5, "million": 10**6, "mn": 10**6, "m": 10**6,
          "k": 10**3, "thousand": 10**3, "bn": 10**9, "billion": 10**9}

# A number not glued to a word before it (so "FY25" and "X12" are ignored), then an optional
# scale word or a percent sign.
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s*(%|crores?|cr|lakhs?|lacs?|million|mn|thousand|billion|bn|k|m)\b|\s*(%))?",
    re.IGNORECASE)
_LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)  # "1. " at the start of a line
EXEMPT_UP_TO = Decimal(10)


@dataclass(frozen=True)
class Written:
    """A number as it appears in the answer."""

    raw: str
    value: Decimal
    scale: int  # 1, 10**5, 10**7, ...
    percent: bool
    decimals: int


def written_numbers(text: str) -> list[Written]:
    text = _LIST_MARKER.sub(" ", text)
    out = []
    for m in _NUMBER.finditer(text):
        digits, unit = m.group(1), (m.group(2) or m.group(3) or "").lower()
        value = Decimal(digits.replace(",", ""))
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        out.append(Written(raw=m.group(0).strip(), value=value,
                           scale=SCALES.get(unit, 1), percent=unit == "%", decimals=decimals))
    return out


def _numbers_in_string(text: str) -> list[Decimal]:
    """Numbers inside text, both as written and at their scale: "Rs 3.5 Cr" gives 3.5 and
    35000000, so an answer repeating a vendor's own "3.5 Cr" is grounded (UX audit H9)."""
    out = []
    for w in written_numbers(text):
        out.append(w.value)
        if w.scale != 1:
            out.append(w.value * w.scale)
    return out


def tool_numbers(results: list) -> set[Decimal]:
    """Every number found anywhere in the tool results (nested JSON, numeric strings, text)."""
    found: set[Decimal] = set()

    def walk(x):
        if isinstance(x, bool) or x is None:
            return
        if isinstance(x, int | float | Decimal):
            try:
                found.add(abs(Decimal(str(x))))
            except InvalidOperation:
                pass
        elif isinstance(x, str):
            try:
                found.add(abs(Decimal(x)))
            except InvalidOperation:
                found.update(_numbers_in_string(x))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list | tuple):
            for v in x:
                walk(v)

    for r in results:
        walk(json.loads(r) if isinstance(r, str) and r[:1] in "[{" else r)
    return found


def _rounds_to(source: Decimal, w: Written) -> bool:
    quantum = Decimal(1).scaleb(-w.decimals)
    candidates = [source / w.scale]
    if w.percent:
        candidates.append(source * 100)  # a share 0.12 written as 12%
    return any(c.quantize(quantum, rounding=ROUND_HALF_UP) == w.value or
               c.quantize(quantum, rounding="ROUND_DOWN") == w.value for c in candidates)


def unmatched(answer: str, results: list, question: str = "") -> list[str]:
    """Numbers written in the answer that no tool result supports. Empty = grounded."""
    sources = tool_numbers(results)
    asked = {w.value * w.scale for w in written_numbers(question)}
    bad = []
    for w in written_numbers(answer):
        full = w.value * w.scale
        if w.scale == 1 and not w.percent and w.decimals == 0 and w.value <= EXEMPT_UP_TO:
            continue
        if full in asked or w.value in asked:
            continue
        if not any(_rounds_to(s, w) for s in sources):
            bad.append(w.raw)
    return bad
