"""T16: compare attached certificates and reports with the vendor's questionnaire answers.

A question is linked to a document when a code or acronym in the question ("ISO 9001",
"FSC") also appears in the document's type, standard or scope. "Yes, certified" with an
expired or missing document opens an attachment_conflict flag; the gating verdict then
uses the document's date, not the vendor's word.
"""

from __future__ import annotations

import re
from datetime import date

from level.core.gating import parse_date, parse_yes_no
from level.types import DocumentFacts

_CODE = re.compile(r"\b[A-Z][A-Z0-9-]{1,}(?:\s?\d{3,5})?\b")
_IGNORE = {"QA", "QC", "ID", "NO", "YES", "OK", "RFX", "RFQ", "PO", "GST", "INR", "USD"}


def codes(text: str) -> set[str]:
    """Upper-case codes in a question: {'ISO 9001', 'FSC'}."""
    return {c.replace(" ", "") for c in _CODE.findall(text or "") if c not in _IGNORE}


def linked_documents(question_text: str, docs: list[DocumentFacts]) -> list[DocumentFacts]:
    wanted = codes(question_text)
    if not wanted:
        return []
    out = []
    for d in docs:
        hay = " ".join(x or "" for x in (d.document_type, d.standard, d.scope)).upper()
        hay = hay.replace(" ", "")
        if any(code.upper() in hay for code in wanted):
            out.append(d)
    return out


def latest_expiry(docs: list[DocumentFacts]) -> date | None:
    dates = [parse_date(d.expiry_date) for d in docs if d.expiry_date]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def conflict(question_text: str, answer: str | None, docs: list[DocumentFacts],
             cutoff: date) -> str | None:
    """A plain reason when the answer and the attached documents disagree, else None."""
    if parse_yes_no(answer) is not True or not codes(question_text):
        return None
    linked = linked_documents(question_text, docs)
    if not linked:
        if re.search(r"\battach", question_text, re.IGNORECASE):
            return "answered yes, but no matching document was attached"
        return None
    expiry = latest_expiry(linked)
    if expiry is None:
        return None
    if expiry < cutoff:
        return (f"answered yes, but the attached {linked[0].document_type} expired on "
                f"{expiry.isoformat()} (needed valid on {cutoff.isoformat()})")
    return None
