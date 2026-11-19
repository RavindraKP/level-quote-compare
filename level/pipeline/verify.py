"""Stage 4: verify. T1 evidence check (code), T2 independent second read (model), T3 hooks.

T1: the verbatim quote must be found in the block the locator names (exact after whitespace
and punctuation clean-up; a fuzzy match is allowed for PDF text, whose extraction can split
ligatures), and the price digits must appear in the quote. For spreadsheets the cell at the
cited address must hold the price. Image sources have no text to match: their check is the
double read in run.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher

from level import llm
from level.core.checks import check_line_amount, check_total
from level.core.normalise import parse_number
from level.pipeline import prompts
from level.pipeline.intake import Block
from level.types import ExtractionOutput, SecondReadOutput

FUZZY_MIN = 0.90
_TAG = re.compile(r"\[[^\]]*\]\s?")  # "[Sheet1!C4] " / "[paragraph 7] " labels added by intake


def clean(text: str) -> str:
    """Lower-case, drop our labels, unify quotes/dashes, cell separators and whitespace.

    A PDF table row comes out of text extraction one cell per line, while the model quotes
    it as "1 | Item | 20.90"; both reduce to "1 item 20.90".
    """
    text = _TAG.sub("", text or "")
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"')
    text = text.replace("”", '"').replace("–", "-").replace("—", "-")
    text = re.sub(r"[|\t]", " ", text)  # table-cell separators are layout, not content
    text = text.replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def digits(text: str | None) -> str:
    return re.sub(r"[^\d]", "", text or "")


def price_in_quote(price: str | None, quote: str) -> bool:
    """The price's digits must appear in the quote ('1,250.00' in 'Rs 1250.00/pc')."""
    if price is None:
        return True
    d = digits(price).rstrip("0") or digits(price)
    return bool(d) and d in digits(quote)


def fuzzy_contains(needle: str, hay: str) -> bool:
    """Best matching window of the same length reaches FUZZY_MIN similarity."""
    if not needle or len(needle) > len(hay) + 5:
        return False
    best = 0.0
    step = max(1, len(needle) // 4)
    for start in range(0, max(1, len(hay) - len(needle) + 1), step):
        window = hay[start:start + len(needle) + 5]
        best = max(best, SequenceMatcher(None, needle, window).ratio())
        if best >= FUZZY_MIN:
            return True
    return False


@dataclass
class EvidenceResult:
    ok: bool | None  # None = no text to check against (image source)
    locator: str  # possibly corrected to the block where the quote was actually found
    detail: str


def _block_for(locator: str, blocks: list[Block]) -> Block | None:
    loc = locator.strip().lower()
    sheet = re.match(r"'?([^'!]+)'?!([a-z]+\d+)", loc)
    for b in blocks:
        label = b.label.lower()
        if sheet and label == sheet.group(1).strip():
            return b
        if label == loc or loc.startswith(label + " ") or loc == label.replace(",", ""):
            return b
    return None


def check_evidence(price: str | None, quote: str, locator: str, blocks: list[Block],
                   fuzzy: bool = False) -> EvidenceResult:
    """T1 for one value. Falls back to other blocks when the locator is slightly off."""
    text_blocks = [b for b in blocks if b.text]
    if not text_blocks:
        return EvidenceResult(None, locator, "image source: checked by the double read")
    if not quote or not quote.strip():
        return EvidenceResult(False, locator, "no quote given")
    if not price_in_quote(price, quote):
        return EvidenceResult(False, locator, f"price {price} does not appear in the quote")
    needle = clean(quote)
    cited = _block_for(locator, text_blocks)
    sheet_cell = re.match(r"'?([^'!]+)'?!([A-Za-z]+\d+)", locator.strip())
    if cited is not None and sheet_cell and price is not None:
        cell = re.search(rf"\[{re.escape(sheet_cell.group(0))}\] ([^|\n]*)", cited.text,
                         re.IGNORECASE)
        if cell is None or not price_in_quote(price, cell.group(1)):
            return EvidenceResult(False, locator,
                                  f"cell {sheet_cell.group(0)} does not hold {price}")
    order = ([cited] if cited else []) + [b for b in text_blocks if b is not cited]
    for b in order:
        hay = clean(b.text)
        if needle in hay or (fuzzy and fuzzy_contains(needle, hay)):
            if b is cited:
                return EvidenceResult(True, locator, "quote found at the cited place")
            if sheet_cell:  # a cell address in the wrong sheet is not a near miss
                continue
            return EvidenceResult(True, b.label,
                                  f"quote found in {b.label} (cited '{locator}')")
    return EvidenceResult(False, locator, "quoted words not found in the source")


# ---------------------------------------------------------------------------
# T2 second read (text sources). Images get a full second extraction instead.
# ---------------------------------------------------------------------------


def second_read(content: list[dict], extraction: ExtractionOutput
                ) -> tuple[SecondReadOutput, llm.LLMResult]:
    rows = "\n".join(
        f"{i}: item={r.item_text!r} price={r.price!r} currency={r.currency!r} "
        f"unit={r.uom_text!r}" for i, r in enumerate(extraction.rows))
    message = content + [{"type": "text", "text": "Extracted rows to check:\n" + llm.untrusted(
        "extracted rows", rows or "(none)")}]
    result = llm.structured(SecondReadOutput, [{"role": "user", "content": message}],
                            system=prompts.SECOND_READ_SYSTEM, effort="low", max_tokens=8000)
    return result.parsed, result


# ---------------------------------------------------------------------------
# T3 arithmetic on what the vendor wrote
# ---------------------------------------------------------------------------


@dataclass
class ArithmeticIssue:
    row_index: int | None  # None for a document-level total
    detail: str


def arithmetic_issues(extraction: ExtractionOutput) -> list[ArithmeticIssue]:
    """qty x rate vs amount per row; rows vs each stated total."""
    issues = []
    amounts: list[Decimal] = []
    for i, row in enumerate(extraction.rows):
        qty, rate, amount = (parse_number(row.quantity), parse_number(row.price),
                             parse_number(row.amount))
        multiple = parse_number(row.basis.multiple) if row.basis and row.basis.multiple else None
        if amount is not None:
            amounts.append(amount)
        elif qty is not None and rate is not None:
            amounts.append(qty * rate / (multiple or 1))
        if qty is not None and rate is not None and amount is not None:
            check = check_line_amount(qty, rate / (multiple or 1), amount)
            if not check.passed:
                issues.append(ArithmeticIssue(i, check.detail))
    priced = sum(1 for r in extraction.rows if r.price is not None)
    if len(amounts) < priced:  # some rows have no amount: a sum would be partial
        return issues
    for total in extraction.stated_totals:
        stated = parse_number(total.amount)
        # Sub-totals and tax-inclusive totals are not comparable with the plain row sum.
        if stated is None or not amounts or re.search(r"sub|gst|tax|vat", total.label, re.I):
            continue
        check = check_total(amounts, stated)
        if not check.passed:
            issues.append(ArithmeticIssue(None, f"{total.label}: {check.detail}"))
    return issues
