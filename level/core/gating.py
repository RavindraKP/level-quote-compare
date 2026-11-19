"""Questionnaire verdicts decided by code from each question's pass rule.

A missing or unreadable answer is "unclear", never "pass". Rules are JSON on the question:
    {"op": "==", "value": "yes"}            yes/no questions
    {"op": "<=", "value": "10"}             numeric thresholds (also <, >, >=)
    {"op": "valid_on", "value": "2026-04-01"}  a date (certificate expiry) must be on/after it
    {"op": "contains", "value": "FSC"}      free text must mention something
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from level.core.normalise import parse_number

Verdict = tuple[str, str]  # (pass / fail / unclear, rationale)

_YES = re.compile(r"^\s*(yes|y|yeah|yep|we do|we have|available|certified|accepted|agreed|"
                  r"ok|okay|true)\b", re.IGNORECASE)
_NO = re.compile(r"^\s*(no|n|not|nope|we do not|we don't|none|nil|false|na|n/a)\b",
                 re.IGNORECASE)

_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d %b %Y",
                 "%d %B %Y", "%b %d %Y", "%B %d %Y", "%d-%b-%Y", "%d-%b-%y", "%b %Y", "%B %Y"]


_LABEL = re.compile(r"^\s*[A-Za-z][^:\n]{0,60}[A-Za-z)]\s*:\s*(.+)$", re.DOTALL)  # "ISO: yes"


def parse_yes_no(text: str | None) -> bool | None:
    """'Yes, copy enclosed' -> True; 'No' -> False; also 'ISO certified: yes' (a label, then
    the answer). Anything else -> None (unclear)."""
    if not text:
        return None
    if _NO.search(text):
        return False
    if _YES.search(text):
        return True
    labelled = _LABEL.match(text)
    if labelled and labelled.group(1).strip() != text.strip():
        return parse_yes_no(labelled.group(1))
    return None


def parse_date(text: str | None) -> date | None:
    """First date found in the text. Day-first for numeric dates (Indian convention)."""
    if not text:
        return None
    if isinstance(text, date):
        return text
    candidates = re.findall(
        r"\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|"
        r"\d{1,2}[ -][A-Za-z]{3,9},?[ -]\d{2,4}|[A-Za-z]{3,9} \d{1,2},? \d{4}|[A-Za-z]{3,9} \d{4}",
        str(text),
    )
    for cand in candidates:
        cleaned = cand.replace(",", "")
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(cleaned, fmt).date()
            except ValueError:
                continue
    return None


def verdict(answer: str | None, rule: dict | None, *, expiry: date | None = None,
            stance: str | None = None) -> Verdict:
    """Pass / fail / unclear for one answer against one rule.

    expiry: a date taken from an attached document (certificate), which beats the answer text.
    stance: the reader's yes/no reading of a prose answer ("We test every lot in-house" reads
    as yes). Used only when the text itself has no plain yes or no, and said in the rationale.
    """
    if not rule:
        return ("unclear", "no pass rule") if not answer else ("pass", "not a gating question")
    op = str(rule.get("op", "")).strip().lower()
    target = rule.get("value")

    if op == "valid_on":
        cutoff = parse_date(str(target))
        found = expiry or parse_date(answer)
        if cutoff is None:
            return "unclear", f"rule date '{target}' could not be read"
        if found is None:
            return "unclear", "no expiry date stated or attached"
        source = "attached document" if expiry else "answer"
        if found >= cutoff:
            return "pass", (f"valid until {found.isoformat()} ({source}); "
                            f"needed {cutoff.isoformat()}")
        return "fail", f"expires {found.isoformat()} ({source}), before {cutoff.isoformat()}"

    if answer is None or not str(answer).strip():
        return "unclear", "not answered"

    if op in ("==", "eq", "is") and str(target).lower() in ("yes", "no", "true", "false"):
        wanted = str(target).lower() in ("yes", "true")
        got = parse_yes_no(answer)
        said = "answered"
        if got is None and stance in ("yes", "no"):
            got, said = stance == "yes", "the vendor's wording reads as"
        if got is None:
            return "unclear", f"answer '{answer}' is not a clear yes or no"
        return ("pass", f"{said} {'yes' if got else 'no'}") if got == wanted else (
            "fail", f"{said} {'yes' if got else 'no'}; needed {'yes' if wanted else 'no'}")

    if op in ("<=", "<", ">=", ">", "=="):
        got_n = parse_number(answer)
        want = parse_number(str(target))
        if got_n is None or want is None:
            return "unclear", f"no number in answer '{answer}'"
        ok = _compare(got_n, op, want)
        return ("pass" if ok else "fail"), f"{got_n} {op} {want} is {'true' if ok else 'false'}"

    if op == "contains":
        ok = str(target).lower() in str(answer).lower()
        said = "mentions" if ok else "does not mention"
        return ("pass" if ok else "fail"), f"answer {said} {target}"

    return "unclear", f"unknown rule '{op}'"


def _compare(a: Decimal, op: str, b: Decimal) -> bool:
    return {"<=": a <= b, "<": a < b, ">=": a >= b, ">": a > b, "==": a == b}[op]


def vendor_gating(verdicts: list[str]) -> str:
    """One vendor's overall gating result: fail beats unclear beats pass."""
    if "fail" in verdicts:
        return "fail"
    if "unclear" in verdicts or not verdicts:
        return "unclear"
    return "pass"
