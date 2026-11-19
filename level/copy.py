"""Every word a buyer or vendor reads, in one place (docs/UX_GUIDE.md section 2).

Pages, exports and emails take their labels from here, so wording changes in one file.
Nothing here computes a price: the formatters only round and group numbers for display.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from level.types import CellState, FlagType, Severity

# --- Cell states ---------------------------------------------------------------------------
# Colour is never the only signal: each state has a symbol and a word as well.

STATE_LABEL = {
    CellState.CONFIRMED: "Verified",
    CellState.INFERRED: "Assumed",
    CellState.NEEDS_REVIEW: "Check",
    CellState.CONFLICT: "Mismatch",
    CellState.NOT_QUOTED: "Not quoted",
    CellState.AWAITING: "Awaiting reply",
}
STATE_SYMBOL = {
    CellState.CONFIRMED: "✓",
    CellState.INFERRED: "≈",
    CellState.NEEDS_REVIEW: "⚠",
    CellState.CONFLICT: "✖",
    CellState.NOT_QUOTED: "—",
    CellState.AWAITING: "…",
}
STATE_HELP = {
    CellState.CONFIRMED: "The price was found in the vendor's document and passed every check.",
    CellState.INFERRED: "The price rests on an assumption, such as last year's price or a "
                        "conversion. It can be compared, but read the note.",
    CellState.NEEDS_REVIEW: "Something about this price needs a look before it can be compared.",
    CellState.CONFLICT: "Two figures disagree. It is not compared until someone decides.",
    CellState.NOT_QUOTED: "The vendor gave no price for this line. It is never counted as zero.",
    CellState.AWAITING: "The vendor has not replied yet.",
}

# --- Issues (what the code calls flags) ----------------------------------------------------
# Title: what is wrong. Why: why it matters. Todo: what to do. One line each.

ISSUE_TITLE = {
    FlagType.EVIDENCE: "Price not found in the document",
    FlagType.READ_MISMATCH: "Hard to read",
    FlagType.ARITHMETIC: "Figures do not add up",
    FlagType.PLAUSIBILITY: "Much higher or lower than other quotes",
    FlagType.SPEC_DEVIATION: "Offered item differs from the specification",
    FlagType.ATTACHMENT_CONFLICT: "Documents disagree",
    FlagType.MISSING_INFO: "Information missing",
    FlagType.REFERENCE: "Price taken from last year",
    FlagType.NO_HISTORY: "Vendor referred to last year, but did not supply this item then",
    FlagType.SUSPICIOUS_INSTRUCTION: "Document contains instructions to the reader",
    FlagType.UNREADABLE: "File could not be opened",
}
ISSUE_WHY = {
    FlagType.EVIDENCE: "A price we cannot point to in the vendor's own words is not compared.",
    FlagType.READ_MISMATCH: "Two readings give different numbers; the wrong one would mislead.",
    FlagType.ARITHMETIC: "The vendor's own totals and unit prices disagree.",
    FlagType.PLAUSIBILITY: "Often a different unit or pack size; comparing it as is would mislead.",
    FlagType.SPEC_DEVIATION: "A different item may not be a fair comparison.",
    FlagType.ATTACHMENT_CONFLICT: "The questionnaire answer may be wrong.",
    FlagType.MISSING_INFO: "Without it, part of the price or terms is an assumption.",
    FlagType.REFERENCE: "The price is assumed from our records until the vendor confirms it.",
    FlagType.NO_HISTORY: "We have nothing to take a price from, so the line is not quoted.",
    FlagType.SUSPICIOUS_INSTRUCTION: "The text was ignored; nothing in it changed the comparison.",
    FlagType.UNREADABLE: "Nothing from this file is in the comparison.",
}
ISSUE_TODO = {
    FlagType.EVIDENCE: "Check the source, then accept the price or correct it.",
    FlagType.READ_MISMATCH: "Look at the source and pick the right value.",
    FlagType.ARITHMETIC: "Pick the right figure, or ask the vendor.",
    FlagType.PLAUSIBILITY: "Check the unit in the source, or ask the vendor.",
    FlagType.SPEC_DEVIATION: "Accept the alternative, or mark the line not quoted.",
    FlagType.ATTACHMENT_CONFLICT: "Check both documents and accept the right answer.",
    FlagType.MISSING_INFO: "Ask the vendor, or accept the assumption.",
    FlagType.REFERENCE: "Ask the vendor to confirm the price still applies.",
    FlagType.NO_HISTORY: "Ask the vendor for a price.",
    FlagType.SUSPICIOUS_INSTRUCTION: "No action needed; noted for the record.",
    FlagType.UNREADABLE: "Ask the vendor for another copy, or add the prices yourself.",
}
SEVERITY_LABEL = {Severity.CRITICAL: "High", Severity.WARNING: "Medium", Severity.INFO: "Low"}

# Buttons say what happens.
REVIEW_ACTIONS = {
    "confirm": "Accept price",
    "edit_value": "Correct the price",
    "not_quoted": "Mark not quoted",
    "apply_fix": "Use the suggested value",
}
CLOSED_BY = {"checks": "the checks", "buyer": "you", "vendor": "the vendor"}

# --- Event stages --------------------------------------------------------------------------

STAGES = ["Draft", "Issued", "Collecting", "Evaluate", "Awarded"]
STATUS_STAGE = {"draft": "Draft", "issued": "Issued", "collecting": "Collecting",
                "ready": "Evaluate", "awarded": "Awarded"}
VERDICT_LABEL = {"pass": "Meets requirements", "fail": "Does not meet",
                 "unclear": "Unclear", "awaiting": "Awaiting reply"}
VERDICT_SYMBOL = {"pass": "✓", "fail": "✖", "unclear": "?", "awaiting": "…"}

# --- Plain wording for reasons written by the checks ---------------------------------------
# The checks write short technical notes. On screen, the internal words are swapped for
# the buyer's words. Order matters: longer phrases first.

def _drop_initials(m: re.Match) -> str:
    """'bursting_factor_bf' -> 'bursting_factor': a key followed by its own initials."""
    words = m.group(1).split("_")
    return m.group(1) if "".join(w[0] for w in words) == m.group(2) else m.group(0)


_PLAIN = [
    (r"\bNO_BID\b", "not offered"),
    (r"\breply link chat, message (\d+|\?)", "their answer on the reply link"),
    # Last year's price and other assumptions named by key in a cell's reason (UX audit B6)
    (r"\bhistory\.(\w+) = ", r"last year's price (\1): "),
    (r"\bhistory\.(\w+)\b", r"last year's price (\1)"),
    (r"\bunit = no unit stated = 1 (\w+)", r"no unit stated; taken as one \1"),
    (r"\bno unit stated = 1 (\w+)", r"no unit stated; taken as one \1"),
    (r"\bcurrency = no currency stated = (\w+)", r"no currency stated; taken as \1"),
    (r"\bgst_rate = (0\.\d+)\b", lambda m: f"GST rate {pct(Decimal(m.group(1)) * 100)}"),
    (r"\bcost_of_capital = (0\.\d+)\b",
     lambda m: f"cost of capital {pct(Decimal(m.group(1)) * 100)} a year"),
    # Conversion notes written by level/core/normalise.py
    (r"\bfactor\.(\w+)->(\w+) = (multiply|divide) by ", r"conversion per \1 to per \2: \3 by "),
    (r"\bfactor\.(\w+)->(\w+)\b", r"conversion per \1 to per \2"),
    (r"\bunit = 1 (\w+) = 1 (\w+)", r"one \1 taken as one \2"),
    (r"\bfx\.([A-Z]{3})([A-Z]{3}) = ", r"\1 to \2 rate "),
    (r"\b1 unstated = 1 (\w+)", r"no unit stated; taken as one \1"),
    (r"\bunit = 1 (\w+) = 1 (\w+)", r"one \1 taken as one \2"),
    (r"\bgst_rate = ", "GST rate "),
    (r"\bline\.conversion_factors\b", "the line's specification"),
    (r"\bvendor term\b", "the vendor's terms"),
    (r"\s*->\s*", " → "),
    (r"\bper 1 (?=[a-z])", "per "),  # 'per 1 each' -> 'per each' -> 'per piece'
    (r"\bper (\d[\d,]*) each\b", r"per \1 pieces"),
    (r"\bper each\b", "per piece"),
    (r"\bper g\b", "per gram"),
    (r"\bfx\.([A-Z]{3})([A-Z]{3})\b", r"\1 to \2 rate"),
    (r"\blanded \(delivered\)", "delivered"),  # the model's own gloss, not doubled
    (r"\blanded price unknown\b", "delivered price unknown"),
    (r"\blanded unknown\b", "delivered price unknown"),
    (r"; excluded from landed comparisons", "; left out of delivered-price totals"),
    (r"\blanded\b", "delivered"),
    (r"^unclear in the source: ", ""),
    (r"\bevidence check failed: ", "not found in the document: "),
    (r"\bterm evidence not found: ", "term not found in the document: "),
    (r"\bsecond read disagrees on\b", "a second reading disagrees on"),
    (r"\bthe extraction missed\b", "the first reading missed"),
    (r"\bextracted\b", "first reading"),
    (r"\bextraction\b", "reading"),
    (r"\bcanonical unit\b", "comparison unit"),
    (r"\bplausibility\b", "price check"),
    (r"\bcell evidence\b", "source"),
    (r"(?<![\w-])cells\b", "prices"),  # not "12-cell", a product description
    (r"(?<![\w-])cell\b", "price"),
    (r"\bevidence\b", "source"),
    (r"\blocator\b", "place"),
    (r"\bclaims\b", "prices"),
    (r"\bclaim\b", "price"),
    (r"\b(?:is|are|was|were) (also )?flagged\b", r"\1has a point to check"),
    (r"\bflagged\b", "marked to check"),
    (r"\bflagging\b", "marking to check"),
    (r"\bflags\b", "issues"),
    (r"\bflag\b", "issue"),
    (r"\bblocks?\b", "sections"),
    (r"\ba (scenario|award option)\b", "an award option"),
    (r"\bscenarios\b", "award options"),
    (r"\bscenario\b", "award option"),
    (r"\bfailed a gating question\b", "does not meet a requirement"),
    (r"\b(clears?|cleared|clearing|pass(?:es|ed)?|meets?) (?:the )?gating"
     r"(?: requirements?| questions?| checks?)?", "meets the requirements"),
    (r"\bfails? (?:the )?gating(?: requirements?| questions?| checks?)?",
     "does not meet the requirements"),
    (r"\bgating answers\b", "answers to the requirements"),
    (r"\bgating (?:requirements|questions|checks)\b", "requirements"),
    (r"\bgating (?:requirement|question|check)\b", "requirement"),
    (r"(?<=\w's) gating\b", " requirement check"),
    (r"\bgating\b", "requirement"),
    (r"\bnot confirmed\b", "not verified"),
    (r"\bconfirmed\b", "verified"),
    (r"\binferred\b", "assumed"),
    (r"\bneeds[ _]review\b", "to check"),
    (r"\bnot_quoted\b", "not quoted"),
    (r"\bQ(\d+):", r"Question \1:"),
    (r"\b([a-z]+(?:_[a-z]+)+)[_ ]\(?([a-z]{2,4})\)?(?=:)", _drop_initials),
    (r"(?<=[a-z])_(?=[a-z])", " "),  # bursting_factor -> bursting factor
    # A word glossed with itself after the swaps above: 'delivered (delivered)'.
    (r"\b(\w+(?: \w+)?) \(\1\)", r"\1"),
]


# A grouped amount of a lakh or more, written with zero paise and no currency sign: a
# total, shown with the rupee sign and in whole rupees ('3,42,05,850.00' -> '₹3,42,05,850').
# Only amounts written with paise count: '1,00,000 nos' is a quantity and is left alone.
_TOTAL = re.compile(r"(?<![₹\d,.])(?<!Rs )(?<!Rs\. )(?<!INR )(-?)(\d{1,2}(?:,\d{2})+,\d{3})"
                    r"\.00(?![\d,])")


def _total(m: re.Match) -> str:
    return f"{m.group(1)}₹{m.group(2)}"


def plain(text: str | None, capitalise: bool = True, as_written: bool = False) -> str:
    """A reason written by the checks, in the buyer's words. capitalise=False keeps the first
    letter as it is, for text placed in the middle of a sentence. as_written=True is for a
    vendor's own words: internal terms are still swapped, amounts are left as they wrote them."""
    out = text or ""
    for pattern, repl in _PLAIN:
        out = re.sub(pattern, repl, out, flags=0 if "[A-Z]" in pattern else re.I)
    out = _TOTAL.sub(_total, out)
    if not as_written:
        out = _outside_quotes(out, lambda part: money_rounding(_rupee_sign(part)))
    return out[:1].upper() + out[1:] if capitalise else out


# A vendor's own words in quotation marks are shown as written, never reformatted.
_QUOTED = re.compile(r"(“[^”]*”|\"[^\"]*\")")


def _outside_quotes(text: str, fix) -> str:
    return "".join(part if i % 2 else fix(part) for i, part in enumerate(_QUOTED.split(text)))


def _rupee_sign(text: str) -> str:
    """Rupees with the sign, not the code: '42.12 INR' and 'INR 42.12' -> '₹42.12'."""
    text = re.sub(r"(?<![\w.,])(-?\d[\d,]*(?:\.\d+)?) ?INR\b(?!\s*/)", r"₹\1", text)
    return re.sub(r"(?<![/\w])INR\s?(?=-?\d)", "₹", text)


# A rupee amount written with paise or more decimals: '₹3,30,02,493.5', '₹27.1350'.
_RUPEES = re.compile(r"(₹ ?)(-?\d[\d,]*)\.(\d+)(?![\d.])")
TOTAL_FROM = Decimal(100000)  # one lakh: amounts this large are totals, shown in whole rupees


def money_rounding(text: str) -> str:
    """Rupee amounts on screen: totals (a lakh or more) in whole rupees, other amounts to two
    decimals ('₹3,30,02,493.5' -> '₹3,30,02,494', '₹27.1350' -> '₹27.14'). Display only:
    the stored figures and every calculation stay exact. Commas are kept as written."""

    def fix(m: re.Match) -> str:
        whole, frac = m.group(2), m.group(3)
        value = Decimal(whole.replace(",", "") + "." + frac)
        places = 0 if abs(value) >= TOTAL_FROM else 2
        if places == 2 and len(frac) <= 2:
            return m.group(0)
        rounded = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if "," in whole:
            text = number(rounded, places)
        else:
            text = f"{rounded:.{places}f}"
        return m.group(1) + text

    return _RUPEES.sub(fix, text or "")


def is_covering_email_note(reason: str | None) -> bool:
    """A reading note that says only the covering email was in a message ("no prices in this
    file", "attachments mentioned"). Raised on the email itself, it is false once the
    attached files were read, so it is never put to the vendor then (UX audit H7)."""
    r = (reason or "").lower()
    return r.startswith("unclear in the source") and (
        "covering email" in r or "attachment" in r or "attached" in r)


# --- Words that must never reach a screen, an export or an email ---------------------------

# Internal terms (section 2). Whole words, any case.
BANNED_SCREEN = [
    "flag", "flags", "flagged", "claim", "claims", "cell state", "extraction", "locator",
    "evidence quote", "plausibility", "no_history", "read_mismatch", "gating", "scenario",
    "scenarios", "canonical", "inferred", "needs review", "block", "blocks",
]
# Emails also never accuse (section 1).
BANNED_EMAIL = BANNED_SCREEN + ["error", "mismatch", "discrepancy", "invalid", "failed"]


def find_banned(text: str, words: list[str] = BANNED_SCREEN) -> list[str]:
    """The banned words that appear in `text` (whole words, ignoring case)."""
    return [w for w in words if re.search(rf"(?<![\w.-]){re.escape(w)}(?![\w-])", text, re.I)]


# --- Numbers, money, dates -----------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))


def number(value: Decimal | None, decimals: int = 0) -> str:
    """Indian digit grouping without a currency sign: 34205850 -> '3,42,05,850'."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.{decimals}f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:  # Indian grouping: thousands, then pairs (lakh, crore)
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    text = ",".join(groups + [tail]) if groups else tail
    return sign + text + (f".{frac}" if frac else "")


def inr(value: Decimal | None, decimals: int = 0) -> str:
    """Indian digit grouping: 12345678 -> '₹1,23,45,678'. Display only."""
    if value is None:
        return "—"
    text = number(value, decimals)
    return f"-₹{text[1:]}" if text.startswith("-") else f"₹{text}"


def inr_short(value: Decimal | None) -> str:
    """Large totals: '₹3.71 Cr', '₹48.2 L'. The exact figure goes on hover or in a receipt."""
    if value is None:
        return "—"
    if abs(value) >= Decimal("1e7"):
        return f"₹{value / Decimal('1e7'):.2f} Cr"
    if abs(value) >= Decimal("1e5"):
        return f"₹{value / Decimal('1e5'):.1f} L"
    return inr(value)


def unit_price(value: Decimal | None) -> str:
    """Unit prices to two decimals, Indian grouping."""
    return inr(value, 2)


def pct(value: Decimal | float | None) -> str:
    return "—" if value is None else f"{Decimal(str(value)):.1f}%"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def date(ts: str | None) -> str:
    """'17 Nov 2026'. Accepts an ISO date or timestamp; dates are shown in IST."""
    dt = _parse(ts)
    if dt is None:
        return "—"
    dt = dt.astimezone(IST)
    return f"{dt.day} {dt:%b %Y}"  # no %-d: Windows strftime rejects it


def date_time(ts: str | None) -> str:
    """'17 Nov 2026, 2:10 PM IST'."""
    dt = _parse(ts)
    if dt is None:
        return "—"
    dt = dt.astimezone(IST)
    hour = dt.hour % 12 or 12  # no %-I: Windows strftime rejects it
    return f"{dt.day} {dt:%b %Y}, {hour}:{dt:%M %p} IST"


def _ago(days: int) -> str:
    """3 -> '3 days', 75 -> '2 months', 800 -> '2 years'. Whole units, rounded down."""
    if days < 60:
        return plural(days, "day")
    if days < 730:
        return plural(days // 30, "month")
    return plural(days // 365, "year")


def deadline(due: str | None, now: datetime | None = None) -> str:
    """Replies due, relative to today, so an old dataset date does not look like a bug:
    'Replies due 24 Feb 2025 · closes in 2 days', '... · closes today',
    'Replies closed 24 Feb 2025 · 3 days ago'."""
    dt = _parse(due)
    if dt is None:
        return "No reply date set"
    today = (now or datetime.now(UTC)).astimezone(IST).date()
    left = (dt.astimezone(IST).date() - today).days  # due at the end of that day
    if left < 0:
        return f"Replies closed {date(due)} · {_ago(-left)} ago"
    if left == 0:
        return f"Replies due {date(due)} · closes today"
    return f"Replies due {date(due)} · closes in {_ago(left)}"


def event_number(rfx: dict) -> str:
    """The buyer's own reference if the RFx has one, else RFX-<year>-<id>."""
    ref = (rfx.get("terms") or {}).get("ref")
    year = (rfx.get("created_at") or "")[:4] or "0000"
    return ref or f"RFX-{year}-{rfx['id']:03d}"


def buyer_name() -> str:
    """Who signs emails and appears in the header. Set LEVEL_BUYER_NAME to change it."""
    return os.environ.get("LEVEL_BUYER_NAME", "Procurement team")


def buyer_company(rfx: dict | None) -> str:
    return ((rfx or {}).get("terms") or {}).get("buyer") or os.environ.get(
        "LEVEL_BUYER_COMPANY", "")


def possessive(name: str) -> str:
    """'Acme Ltd' -> "Acme Ltd's"; 'Acme Corrugators' -> "Acme Corrugators'"."""
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def plural(n: int, word: str, many: str | None = None) -> str:
    """'1 open issue', '2 open issues'."""
    return f"{n} {word if n == 1 else (many or word + 's')}"


# --- Loading states: name the stage --------------------------------------------------------


def reading_stage(stage: str | None, vendor: str, n_lines: int) -> str:
    """'Reading Acme Ltd's reply: matching rows to your 30 lines.' from a worker stage."""
    text = (stage or "").strip()
    if text.startswith("extracting "):
        what = f"reading {text.removeprefix('extracting ')}"
    elif text.startswith("matching"):
        what = f"matching rows to your {n_lines} lines"
    elif text.startswith("checking prices"):
        what = "checking each price against the document"
    elif text.startswith("checking questionnaire"):
        what = "checking the questionnaire answers"
    elif text in ("starting", "reading files"):
        what = "opening the files"
    elif text == "done":
        what = "finished"
    else:
        what = "waiting to start"
    return f"Reading {possessive(vendor)} reply: {what}."


def read_failure(file_problem: str | None) -> str:
    """What happened and the next step. Never a stack trace."""
    if file_problem and "password" in file_problem.lower():
        return ("This PDF is password-protected. Ask the vendor for an unlocked copy, or "
                "upload a photo of the pages.")
    if file_problem:
        name, _, why = file_problem.partition(": ")
        why = re.sub(r"\s*\(\w*(Error|Exception)\)", "", why)  # no exception class names
        return (f"{name} could not be opened ({why or 'unreadable'}). Ask the vendor for "
                "another copy, or add the prices yourself.")
    return ("This reply could not be read. Try again; if it fails again, add the prices "
            "yourself with Add a reply.")


# --- Assumptions (global inputs on the Compare page) ---------------------------------------

TERM_LABEL = {"price_basis": "Price basis", "currency": "Currency",
              "payment_days": "Payment terms (days)", "delivery": "Delivery",
              "payment_days_requested": "Payment terms (days)", "due": "Reply by",
              "award_date": "Award date"}


def field_label(key: str) -> str:
    """'payment_days' -> 'Payment terms (days)'; 'screen_size' -> 'Screen size'."""
    return TERM_LABEL.get(key) or key.replace("_", " ").strip().capitalize()


ASSUMPTION_LABEL = {"cost_of_capital": "Cost of capital (per year)", "gst_rate": "GST rate",
                    "unit": "Unit", "currency": "Currency"}


def assumption_label(key: str) -> str:
    """'fx.USDINR' -> 'USD to INR rate'; unknown keys in sentence case."""
    if key in ASSUMPTION_LABEL:
        return ASSUMPTION_LABEL[key]
    if key.startswith("fx."):
        return plain(key) if len(key) == 9 else key
    if key.startswith("factor."):  # 'factor.g->each' -> 'Conversion per gram → per piece'
        a, _, b = key.removeprefix("factor.").partition("->")
        return "Conversion " + plain(f"per {a} -> per {b}").lower()
    if key.startswith("history."):
        return "Last year's price"
    return key.replace("_", " ").capitalize()



def assumption_value(key: str, value: str | None) -> str:
    """How an assumption reads on screen: 'gst_rate' 0.18 -> '18.0%', 'fx.USDINR' 86.7 ->
    '₹86.70 per USD'. Display only (UX audit M5)."""
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return str(value or "—")
    if key in ("gst_rate", "cost_of_capital"):
        return pct(number * 100) + (" a year" if key == "cost_of_capital" else "")
    if key.startswith("fx.") and len(key) == 9 and key.endswith("INR"):
        return f"{unit_price(number)} per {key[3:6]}"
    return str(value)

# --- Conversion steps on a receipt --------------------------------------------------------

# Where a step's factor came from, in the buyer's words (ConversionStep.source).
STEP_SOURCE = {"as quoted": "as the vendor quoted", "standard unit": "standard conversion",
               "line.conversion_factors": "from the line's specification",
               "vendor term": "from the vendor's terms", "cost_of_capital": "cost of capital"}


def step_number(value: Decimal) -> str:
    """A figure in a conversion step: two decimals, more only when the figure has them
    (at most four), Indian grouping. '20400.0000' -> '20,400.00', '0.3440' -> '0.344'."""
    exponent = value.normalize().as_tuple().exponent
    places = min(4, max(2, -exponent if isinstance(exponent, int) else 2))
    return number(value, places)


def _group_ints(text: str) -> str:
    """'divide by 1000' -> 'divide by 1,000'; decimals and short numbers are left alone."""
    return re.sub(r"(?<![\d.,])\d{4,}(?![\d.,])", lambda m: number(Decimal(m.group(0))), text)


def step_source(source: str | None) -> str:
    if not source:
        return ""
    return STEP_SOURCE.get(source) or plain(source, capitalise=False)


def conversion_step(label: str, operation: str, value_in: Decimal, value_out: Decimal,
                    source: str | None) -> tuple[str, str]:
    """One receipt step in words: ('Per 1,000 pieces → per piece: 20,400.00 → divide by
    1,000 → 20.40', 'as the vendor quoted'). The figures are the stored ones, rounded for
    display only."""
    text = (f"{_group_ints(plain(label))}: {step_number(value_in)} → "
            f"{_group_ints(plain(operation, capitalise=False))} → {step_number(value_out)}")
    return text, step_source(source)


# --- Why a vendor does not meet a requirement (Compare, Technical) ------------------------

_OP_WORDS = {"<=": "{} or less", "<": "less than {}", ">=": "{} or more", ">": "more than {}",
             "==": "exactly {}"}
_SOURCE_WORDS = {"attached document": "from the attached document", "answer": "from their answer"}


def requirement_reason(rationale: str | None, question: str = "",
                       today: datetime | None = None) -> str:
    """The pass-rule check's note (level/core/gating.py) in plain words: 'expires 2025-01-31
    (attached document), before 2025-03-14' -> 'Certificate expired 31 Jan 2025 (from the
    attached document); it must be valid on 14 Mar 2025'. Derived from the stored note only."""
    r = (rationale or "").strip()
    m = re.fullmatch(r"expires (\d{4}-\d{2}-\d{2}) \(([^)]+)\), before (\d{4}-\d{2}-\d{2})", r)
    if m:
        found, source, cutoff = m.groups()
        thing = "Certificate" if "certif" in question.lower() else "Validity"
        now = (today or datetime.now(UTC)).astimezone(IST).date().isoformat()
        verb = "expired" if found < now else "expires"
        if thing == "Validity":
            verb = "ended" if found < now else "ends"
        where = _SOURCE_WORDS.get(source, source)
        return f"{thing} {verb} {date(found)} ({where}); it must be valid on {date(cutoff)}"
    m = re.fullmatch(r"(answered|the vendor's wording reads as) (yes|no); needed (yes|no)", r)
    if m:
        said = "Answered" if m.group(1) == "answered" else "Their wording reads as"
        return f"{said} {m.group(2)}; this requirement needs {m.group(3)}"
    m = re.fullmatch(r"(-?[\d.]+) (<=|<|>=|>|==) (-?[\d.]+) is false", r)
    if m:
        got, op, want = m.groups()
        return f"Gave {got}; this requirement needs {_OP_WORDS[op].format(want)}"
    m = re.fullmatch(r"answer does not mention (.+)", r)
    if m:
        return f"Their answer does not mention {m.group(1)}"
    return plain(r) if r else "Does not meet this requirement"


# --- Past events on the RFx page (WP13) ----------------------------------------------------

PAST_EVENTS = "Past events"
PAST_EVENTS_EMPTY = ("No past events yet. Events you send to vendors appear here, so a repeat "
                     "can start from last time instead of from scratch.")
PAST_EVENTS_PICK = "Select an event to see its lines, terms and questionnaire."
PAST_COLUMNS = {"number": "Event no.", "title": "Title", "year": "Year", "status": "Status",
                "invited": "Vendors invited", "suppliers": "Awarded to",
                "award_value": "Award value per year", "award_date": "Awarded on"}
PAST_LINE_COLUMNS = {"line_no": "No.", "description": "Description", "canonical_uom": "Unit",
                     "annual_qty": "Annual qty", "plant": "Plant", "awarded_to": "Awarded to",
                     "awarded_price": "Awarded price", "spec": "Specification"}
PAST_READ_ONLY = ("Read only. Starting a draft from this event copies it; the event itself is "
                  "not changed.")
NOT_AWARDED = "Not awarded"
START_FROM = "Start from this event"
START_FROM_HELP = ("Copies the lines, questionnaire, terms and invited vendors into your draft. "
                   "Anything already in your draft is kept. The past event is not changed.")
AWARD_VALUE_BASIS = "Annual quantity × awarded price, ex-GST."
REF_PRICE = "Last year's price"
REF_SUPPLIER = "Last year's supplier"
REF_CAPTION = ("Last year's price and supplier are for you only. They are never shown to "
               "vendors: not in the email, the Excel template or the reply link.")
EVENT_STATUS = {"draft": "Draft", "issued": "Issued", "collecting": "Collecting quotes",
                "ready": "Evaluating", "awarded": "Awarded"}


def started_from(result: dict) -> str:
    """'Started from RFX-2025-001: 30 lines, 12 questions, 4 terms and 5 invited vendors
    copied.'"""
    if not result.get("source"):
        return result.get("message") or "No earlier event to start from."
    parts = [plural(result["imported"], "line"), plural(result["questions"], "question"),
             plural(len(result["terms"]), "term"),
             plural(result["invited_vendors"], "invited vendor")]
    kept = result.get("skipped_existing") or 0
    tail = f" {plural(kept, 'line')} already in your draft kept as they were." if kept else ""
    return (f"Started from {result['source_number']}: {', '.join(parts[:-1])} and {parts[-1]} "
            f"copied.{tail}")


# --- Review queue (docs/UX_GUIDE.md "Review queue") ----------------------------------------

REVIEW_INTRO = ("Each point below needs a decision from you. Vendors with the most money at "
                "stake come first. Every decision is recorded, updates the comparison, and "
                "can be undone.")
REVIEW_FILTER_PRIORITY = "Priority"
REVIEW_FILTER_TYPE = "Issue type"
REVIEW_SHOW_DECIDED = "Show decided"
REVIEW_PREV = "← Previous"
REVIEW_NEXT = "Next →"
REVIEW_FOUND = "What we found"
REVIEW_WHY = "Why it matters"
REVIEW_EVIDENCE = "Evidence"
REVIEW_VENDOR_WROTE = "Vendor wrote:"
REVIEW_NO_WORDS = "There are no vendor words to show for this point; it comes from our checks."
REVIEW_WHOLE_REPLY = "This point is about their reply as a whole; open it below."
REVIEW_WHOLE_EMAIL = "Show their whole email"
REVIEW_DECIDE = "Your decision"
REVIEW_NOTE = "Note for the record (optional)"
REVIEW_UNDO = "Undo"
REVIEW_NOTHING_LEFT = "Every issue is decided. The comparison is up to date."
REVIEW_NONE_MATCH = "No issues match these filters."
REVIEW_SAVED = "Saved. The comparison is updated."
REVIEW_UNDONE = "Undone. The issue is open again."
REVIEW_ASK_INTRO = ("One email covers every open point this vendor can answer, grouped so each "
                    "question is asked once. You see it before anything is sent.")
REVIEW_ASK_NOT_FOR_VENDOR = "Only you can decide this point, so it is not put to the vendor."
REVIEW_SEND = "Send email"
REVIEW_EMAIL_OPEN = "These points stay open until the vendor answers."
PRICE_CONTEXT = "Price check"
PRICE_CONTEXT_NOTE = "For you only. Vendors never see this."
PRICE_EDIT_FIELD = "Price in ₹ per {uom}, ex-GST"


def stake_text(value: Decimal) -> str:
    """'₹4.2 L a year at stake', or '' when no money is attached to the point."""
    return f"{inr_short(value)} a year at stake" if value else ""


def progress(position: int, total: int, left: int) -> str:
    """'4 of 32 · 28 left'."""
    return f"{position} of {total} · {left} left"


def vendor_heading(name: str, open_count: int, stake: Decimal) -> str:
    """'Mahalaxmi Packaging · 7 open · ₹48.2 L at stake'."""
    parts = [name, f"{open_count} open"]
    if stake:
        parts.append(f"{inr_short(stake)} at stake")
    return " · ".join(parts)


def _iso_dates(text: str) -> str:
    """'expired on 2025-01-31' -> 'expired on 31 Jan 2025'."""
    return re.sub(r"\b(\d{4}-\d{2}-\d{2})\b", lambda m: date(m.group(1)), text)


def _short(text: str, limit: int = 90) -> str:
    """Cut at a word boundary, never mid-word (UX audit L8)."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:(") + "…"


HISTORY_KIND = {"contract": "last year's contract price", "quote": "their quote last year"}


def unit_assumed(reason: str | None) -> tuple[str | None, str] | None:
    """'unit assumed: 1 box = 1 piece' -> ('box', 'piece'); 'unit assumed: no unit stated =
    1 piece' -> (None, 'piece'), so no sentence ever says 'one unstated'. None otherwise."""
    m = re.match(r"unit assumed: (?:1 (\w+)|no unit stated) = 1 (\w+)", reason or "")
    if not m:
        return None
    unit = m.group(1)
    return (None if unit in (None, "unstated", "unknown") else unit), m.group(2)


def issue_title(kind: FlagType, reason: str, target_kind: str | None = None) -> str:
    """A short name for the kind of point, finer than ISSUE_TITLE for missing information."""
    r = (reason or "").lower()
    if kind == FlagType.EVIDENCE and target_kind == "term":
        return "Term not found in the document"
    if kind == FlagType.MISSING_INFO:
        if r.startswith("unit assumed"):
            return "Unit assumed"
        if r.startswith("landed price unknown"):
            return "Delivered price unknown"
        if r.startswith("unclear in the source"):
            return "Unclear in the reply"
        if r.startswith("cannot convert"):
            return "Price cannot be converted"
        if "refers to an earlier price" in r:
            return "Does last year's price apply?"
        if "not placed on any line" in r:
            return "Price not placed on a line"
        if "missed" in r or "did not" in r:
            return "Items the first reading missed"
        if r.startswith("no prices found"):
            return "No prices in the reply"
    return ISSUE_TITLE[kind]


def issue_summary(kind: FlagType, reason: str, line_no: int | None, price: Decimal | None,
                  candidates: list[str] | None = None, history_source: str | None = None,
                  vs_median: Decimal | None = None, median: Decimal | None = None,
                  target_kind: str | None = None) -> str:
    """One plain line for the issue list, naming the line and the value:
    'Line 6 · ₹42.12 taken from last year's contract'."""
    where = f"Line {line_no}" if line_no else ""
    money = unit_price(price) if price is not None else ""
    r = reason or ""
    text = ""
    if kind == FlagType.REFERENCE:
        source = "contract" if history_source == "contract" else "quote"
        text = f"{money or 'Price'} taken from last year's {source}"
    elif kind == FlagType.NO_HISTORY:
        text = "no price: they did not supply this line last year"
    elif kind == FlagType.READ_MISMATCH and candidates:
        text = f"hard to read: {' or '.join(candidates)}"
    elif kind == FlagType.EVIDENCE:
        text = ("a term not found in their document" if target_kind == "term"
                else f"{money or 'price'} not found in their document")
    elif kind == FlagType.PLAUSIBILITY and vs_median is not None and median is not None:
        text = f"{money} is {vs_median}× the median of other quotes ({unit_price(median)})"
    elif kind == FlagType.SPEC_DEVIATION:
        m = re.search(r"\((.*)\)\s*$", r)
        text = f"offered item differs: {plain(m.group(1)) if m else plain(r)}"
    elif kind == FlagType.MISSING_INFO:
        ua = unit_assumed(r)
        if ua:
            text = (f"{money} assumes one {ua[0]} is one {ua[1]}" if ua[0] else
                    f"{money} taken as per {ua[1]}: no unit stated").strip()
        elif r.startswith("landed price unknown: "):
            why = r.removeprefix("landed price unknown: ").split("; excluded")[0]
            why = re.sub(r"\s*\('[^']*'\)", "", why)  # their words are on the card
            text = f"Delivered price unknown: {why}"
        elif r.startswith("unclear in the source: "):
            text = f"Unclear in the reply: {r.removeprefix('unclear in the source: ')}"
        elif "refers to an earlier price" in r:
            text = "does last year's price apply? The reply does not say"
        else:
            text = plain(r)
    elif kind == FlagType.ATTACHMENT_CONFLICT:
        m = re.match(r"Q(\d+): answered (\w+), but the attached (.+?) expired on (\S+?)"
                     r"(?: \(|$)", r)
        text = (f"Question {m.group(1)}: {m.group(2)}, but the {m.group(3)} expired "
                f"{m.group(4)}" if m else "an answer and its attached document disagree")
    elif kind == FlagType.SUSPICIOUS_INSTRUCTION:
        text = "Text addressed to the reader was ignored"
    elif kind == FlagType.UNREADABLE:
        text = f"{r.partition(': ')[0]} could not be opened"
    else:
        text = plain(r)
    text = _iso_dates(text)
    if where:
        text = f"{where} · {text[:1].lower() + text[1:] if not text.startswith('₹') else text}"
    return _short(text[:1].upper() + text[1:])


def issue_found(kind: FlagType, reason: str, vendor: str, line_no: int | None,
                price: Decimal | None, uom: str | None, rule_text: str | None = None,
                history_source: str | None = None, period: str | None = None,
                vs_median: Decimal | None = None, median: Decimal | None = None) -> str:
    """'What we found', in one sentence."""
    per = f" per {uom}" if uom else ""
    line = f"line {line_no}" if line_no else "this reply"
    if kind == FlagType.REFERENCE and price is not None:
        what = HISTORY_KIND.get(history_source or "", "earlier price")
        when = f" ({period})" if period else ""
        return (f"{vendor} wrote “{rule_text or 'same as last year'}”, so {line} uses "
                f"{what}{when}: {unit_price(price)}{per}.")
    if kind == FlagType.PLAUSIBILITY and vs_median is not None and median is not None:
        return (f"{possessive(vendor)} price for {line}, {unit_price(price)}{per}, is "
                f"{vs_median} times the median of the other quotes ({unit_price(median)}); "
                "this is often a different unit or pack size.")
    ua = unit_assumed(reason)
    if kind == FlagType.MISSING_INFO and ua and ua[0] is None:
        return (f"{vendor} gave no unit for {line}, so we took the price as per {ua[1]}"
                + (f": {unit_price(price)}{per}." if price is not None else "."))
    if kind == FlagType.MISSING_INFO and ua:
        unit, canon = ua
        return (f"{vendor} priced {line} per {unit} without saying how many "
                f"{canon}s are in one {unit}, so we took one {unit} as one "
                f"{canon}" + (f": {unit_price(price)}{per}." if price is not None else "."))
    if kind == FlagType.MISSING_INFO and (reason or "").startswith("landed price unknown: "):
        why = reason.removeprefix("landed price unknown: ").split("; excluded")[0]
        return (f"We cannot work out {possessive(vendor)} delivered price: {why}. Their "
                "prices are left out of delivered-price totals until this is known.")
    return _iso_dates(plain(reason)).rstrip(".") + "."


def accept_label(kind: FlagType, price: Decimal | None, on_line: bool,
                 history_source: str | None = None, reason: str = "") -> str:
    """The accept button, naming what is accepted: 'Accept ₹42.12 (last year's contract
    price)'."""
    if on_line and price is not None:
        if kind == FlagType.REFERENCE:
            kind_text = HISTORY_KIND.get(history_source or "", "last year")
            return f"Accept {unit_price(price)} ({kind_text})"
        ua = unit_assumed(reason)
        if ua:
            return (f"Accept {unit_price(price)} (one {ua[0]} as one {ua[1]})" if ua[0] else
                    f"Accept {unit_price(price)} per {ua[1]}")
        if kind == FlagType.READ_MISMATCH:
            return f"Keep {unit_price(price)} as first read"
        return f"Accept {unit_price(price)} as read"
    return {
        FlagType.ATTACHMENT_CONFLICT: "Keep the answer and its result",
        FlagType.SUSPICIOUS_INSTRUCTION: "Close: the text stays ignored",
        FlagType.UNREADABLE: "Close: leave this file out",
        FlagType.ARITHMETIC: "Keep the line prices as read",
        FlagType.EVIDENCE: "Keep this term as read",
        FlagType.NO_HISTORY: "Accept: the line stays not quoted",
        FlagType.MISSING_INFO: "Close: keep their reply as read",
        FlagType.READ_MISMATCH: "Close: keep the price as first read",
    }.get(kind, "Close: keep their reply as read")


def edit_label() -> str:
    return "Enter a different price"


def save_price_label(line_no: int | None) -> str:
    return f"Save this price for line {line_no}" if line_no else "Save this price"


def not_quoted_label(line_no: int | None) -> str:
    return f"Mark line {line_no} as not quoted" if line_no else "Mark as not quoted"


def ask_label(vendor: str) -> str:
    return f"Ask {vendor}"


def use_candidate_label(value: str) -> str:
    return f"Use {value} (as written)"


def bulk_offer(n: int, vendor: str, title: str) -> str:
    """'5 open issues for Mahalaxmi Packaging have the same cause: price taken from last
    year.'"""
    return f"{n} open issues for {vendor} have the same cause: {title.lower()}."


def bulk_button(n: int) -> str:
    return f"Accept all {n}"


def bulk_title(n: int, vendor: str) -> str:
    return f"Accept {n} prices for {vendor}"


def bulk_confirm(n: int) -> str:
    return f"Accept these {n} prices"


def last_decision_text(action: str, count: int, line_no: int | None, vendor: str) -> str:
    """'Last decision: accepted the price on line 6 (Mahalaxmi Packaging).'"""
    if count > 1:
        what = {"confirm": f"accepted {count} prices", "not_quoted":
                f"marked {count} lines not quoted"}.get(action, f"decided {count} points")
    else:
        on = f" on line {line_no}" if line_no else ""
        what = {"confirm": f"accepted{on or ' a point'}", "edit_value": f"entered a price{on}",
                "not_quoted": f"marked line {line_no} not quoted" if line_no else
                "marked not quoted"}.get(action, "decided a point")
    return f"Last decision: {what} ({vendor})."


def decided_text(action: str | None, closed_by: str | None, value: str | None) -> str:
    """'Decided by you: price accepted.' for a closed issue."""
    who = CLOSED_BY.get(closed_by or "", "someone")
    what = {"confirm": "accepted", "edit_value": f"price set to {unit_price(Decimal(value))}"
            if value else "price corrected", "not_quoted": "marked not quoted"}.get(
        action or "", "settled")
    return f"Decided by {who}: {what}."


def record_text(price: Decimal, uom: str | None, source: str, period: str,
                when: str | None) -> str:
    """'₹42.12 per piece · contract price · FY25, awarded 15 Mar 2024'."""
    per = f" per {uom}" if uom else ""
    kind = "contract price" if source == "contract" else "their quote (did not win)"
    stamp = period + (f", {'awarded' if source == 'contract' else 'quoted by'} {date(when)}"
                      if when else "")
    return f"{unit_price(price)}{per} · {kind} · {stamp}"


# --- Group A labels (UX audit): RFx terms and specs, the event picker, the vendor page -------

TERM_LABEL.update({"basis": "Price basis", "plants": "Delivery plants", "buyer": "Buyer",
                   "ref": "Event no.", "category": "Category", "issued": "Issued on",
                   "validity_days": "Quote valid for (days)", "period": "Period"})

# Short words written in capitals when they are a whole part of an attribute name.
_ACRONYMS = {"sku", "gsm", "bf", "id", "hsn", "iso", "fsc", "ect", "rsc"}
# Unit suffixes on attribute names: 'length_mm' -> 'Length (mm)'.
_UNIT_SUFFIX = {"mm", "cm", "m", "in", "g", "kg", "gsm", "ml", "l"}


def spec_label(key: str) -> str:
    """An attribute name as words: 'length_mm' -> 'Length (mm)', 'sku' -> 'SKU'."""
    if key in TERM_LABEL:
        return TERM_LABEL[key]
    parts = [p for p in key.split("_") if p]
    unit = parts.pop() if len(parts) > 1 and parts[-1].lower() in _UNIT_SUFFIX else None
    words = [p.upper() if p.lower() in _ACRONYMS else p for p in parts] or [key]
    text = " ".join(words)
    text = text[:1].upper() + text[1:]
    return f"{text} ({unit})" if unit else text


def spec_text(spec: dict | None, sep: str = " · ") -> str:
    """A specification as one readable line. Length, width and height with the same unit
    become one size: 'Style RSC · Size 430 × 240 × 190 mm · Board (gsm) 150/120/150'."""
    spec = dict(spec or {})
    sizes = {}  # 'mm' -> its three keys, when length, width and height are all given
    for key in spec:
        unit = key.removeprefix("length_")
        dims = [f"{d}_{unit}" for d in ("length", "width", "height")]
        if key.startswith("length_") and all(k in spec for k in dims):
            sizes[unit] = dims
    merged = {k for dims in sizes.values() for k in dims}
    out = []
    for key, value in spec.items():
        if key.startswith("length_") and key.removeprefix("length_") in sizes:
            dims = sizes[key.removeprefix("length_")]
            out.append(f"Size {' × '.join(str(spec[k]) for k in dims)} {key[7:]}")
        elif key not in merged:
            out.append(f"{spec_label(key)} {value}")
    return sep.join(out)


def conversion_text(factors: dict | None) -> str:
    """Conversion factors as words: {'g_per_piece': '454'} -> '454 g per piece'."""
    out = []
    for key, value in (factors or {}).items():
        a, mid, b = key.partition("_per_")
        out.append(f"{value} {a.replace('_', ' ')} per {b.replace('_', ' ')}" if mid
                   else f"{spec_label(key)}: {value}")
    return "; ".join(out)


ANSWER_TYPE_LABEL = {"yes_no": "Yes / no", "number": "Number", "date": "Date", "text": "Text",
                     "file": "Document"}


def term_value(value) -> str:
    """A term as plain words: lists joined with commas, ISO dates as '3 Feb 2025'."""
    if isinstance(value, list):
        return ", ".join(map(str, value))
    text = "" if value is None else str(value)
    return date(text) if re.match(r"^\d{4}-\d{2}-\d{2}", text) else text


def term_rows(terms: dict | None, skip: tuple[str, ...] = ("title",)) -> list[dict]:
    """[{'Term': 'Delivery plants', 'Value': 'Pune, Hosur, Baddi'}, ...] for a terms table."""
    return [{"Term": field_label(k), "Value": term_value(v)}
            for k, v in (terms or {}).items() if k not in skip and v not in (None, "", [])]


def event_choice(rfx: dict) -> str:
    """How an event reads in the sidebar picker: 'SF/PKG/FY26/014 · Collecting quotes'; a
    sandbox reads 'Sandbox 12 · Draft'."""
    status = EVENT_STATUS.get(rfx["status"], str(rfx["status"]).capitalize())
    if rfx.get("is_sandbox"):
        return f"Sandbox {rfx['id']} · {status}"
    return f"{event_number(rfx)} · {status}"


# --- Sandbox ("Try it with your own files") -------------------------------------------------
SANDBOX_CHIP = "Sandbox"
SANDBOX_NOTE = ("Sandbox with your own files. Nothing you do here changes the demo event. "
                "Files you add are read by an AI service: do not use confidential documents.")
SANDBOX_START_HELP = ("Opens a separate, empty event for your own RFx lines and vendor quotes. "
                      "The demo event stays as it is.")


def event_stage(rfx: dict, all_replied: bool = False, now: datetime | None = None) -> str:
    """The stepper stage. An event still collecting moves to Evaluate once its reply date has
    passed or every invited vendor has replied (UX audit M7)."""
    stage = STATUS_STAGE.get(rfx["status"], "Draft")
    if stage in ("Issued", "Collecting"):
        due = _parse((rfx.get("terms") or {}).get("due"))
        today = (now or datetime.now(UTC)).astimezone(IST).date()
        if all_replied or (due is not None and due.astimezone(IST).date() < today):
            return "Evaluate"
    return stage


SENT_NOTE = ("Sent. This new event has its own Inbox; replies to it appear there. The pages "
             "still show {current} until you switch.")
VENDOR_DONE = "You confirmed your quote on {when}. Thank you."
# --- Caveats under an answer or an award option (UX audit M2) ------------------------------
# The calculation attaches every caveat; the page shows the few that change the number first,
# then the price basis in one line, and folds vendor terms away under "Terms noted (N)".

CAVEATS_SHOWN = 5
TERM_NOTE = "Term noted: "  # the prefix level/core/scenario.py puts on a vendor term
_BASIS_MARKS = ("delivered price = ", "landed = ", " prices converted at ",
                "reduced to ex-gst", "payment terms costed")


def caveat_parts(caveats: list[str]) -> dict[str, list[str]]:
    """Split caveats into {"main": those that change the number (at most five), "more": the
    rest of those, "basis": how prices are compared, "terms": vendor terms noted}, each in
    plain words and without repeats."""
    out: dict[str, list[str]] = {"main": [], "more": [], "basis": [], "terms": []}
    seen: set[str] = set()
    for c in caveats:
        if c in seen:
            continue
        seen.add(c)
        if c.startswith(TERM_NOTE):
            out["terms"].append(plain(c.removeprefix(TERM_NOTE)))
        elif any(m in c.lower() for m in _BASIS_MARKS):
            out["basis"].append(plain(c))
        else:
            out["main"].append(plain(c))
    out["main"], out["more"] = out["main"][:CAVEATS_SHOWN], out["main"][CAVEATS_SHOWN:]
    return out


def terms_noted(n: int) -> str:
    return f"Terms noted ({n})"


# --- Model-written text on screen (analyst answers) ----------------------------------------

# A number not joined to letters, "-", "/", "_" or "." (so file names and codes are skipped).
_WESTERN = re.compile(r"(?<![\w.,/-])(\d{1,3}(?:,\d{3})+|\d{4,})(\.\d+)?(?![\w,/-]|\.\w)")
# Context that makes a bare run of digits an amount or a quantity (QA pass 3 H2).
_MONEY_BEFORE = re.compile(r"(₹|\brs\.?|\binr|\bqty|\bquantity|\btotal|\bof)\s*$", re.I)
_QTY_AFTER = re.compile(r"^\s*(pieces|pcs|pc|units?|nos|numbers|sets?|pairs?|items?|boxes|"
                        r"packs?|kg|kgs|tonnes?|litres?|metres?|each|per\b)", re.I)


def indian_numbers(text: str) -> str:
    """Regroup numbers written the western way ('₹50,756,767', '290000 pieces') in Indian
    style ('₹5,07,56,767', '2,90,000 pieces'). Only the commas move; no digit changes (UX
    audit M1). A bare run of digits is regrouped only next to ₹/Rs/INR or a quantity word,
    so file names, PO, phone and PIN numbers and dates stay as written (QA pass 3 H2)."""

    def regroup(m: re.Match) -> str:
        digits = m.group(1).replace(",", "")
        if "," not in m.group(1):
            if len(digits) <= 4:
                return m.group(0)  # 2026, 1000: leave as written
            if not (_MONEY_BEFORE.search(m.string[:m.start()])
                    or _QTY_AFTER.match(m.string[m.end():])):
                return m.group(0)  # a code, not an amount: "PO 4500012345", "pin 411001"
        return number(Decimal(digits)) + (m.group(2) or "")

    return _outside_quotes(_WESTERN.sub(regroup, text or ""), money_rounding)


COLUMN_LABEL = {"vendor": "Vendor", "line_no": "Line", "description": "Description",
                "total_inr": "Annual value (₹)", "line_total_inr": "Line total (₹)",
                "unit_price_inr": "Unit price (₹)", "landed_price_inr": "Delivered price (₹)",
                "annual_qty": "Annual qty", "state": "Status", "lines_won": "Lines won",
                "unconfirmed_lines": "Lines not yet verified"}


def column_label(name: str) -> str:
    """A tool's column name as a table or chart heading: 'total_inr' -> 'Annual value (₹)'."""
    if name in COLUMN_LABEL:
        return COLUMN_LABEL[name]
    words = re.sub(r"_inr$", " (₹)", name).replace("_", " ").strip()
    return words[:1].upper() + words[1:]



# --- Review card wording for points that need it (UX audit M9, M10, H7, L3) ----------------

TERM_WHY = "A term we cannot point to in the vendor's own words is not applied to the award."
COVERING_EMAIL_WHY = "Nothing to ask the vendor: the note describes the email, not the files."
ALREADY_DECIDED = "Already decided in another window. The next open point is shown."
REVIEW_NOT_READ = ("These replies have not been read yet. Read them on the Inbox page; "
                   "points to check appear here as they are found.")


def term_not_found(vendor: str, words: str) -> str:
    """'We could not find this sentence in their document: “...”.'"""
    if not words:
        return (f"A term in {possessive(vendor)} reply could not be found in their document "
                "as read.")
    words = _short(words, 160)
    return (f"We could not find this sentence in {possessive(vendor)} document: “{words}”"
            + ("" if words.endswith((".", "…")) else "."))


def covering_email_found(vendor: str, reason: str) -> str:
    """H7: a note about the covering email, when their attached files were read anyway."""
    note = plain(reason).removeprefix("Unclear in the reply: ")
    return (f"A note about {possessive(vendor)} covering email: {note.rstrip('.')}. Their "
            "attached files were read separately, so their prices are in the comparison.")


def folded_note(reason: str) -> str:
    """The unit point folded into a price check point on the same line (M9)."""
    ua = unit_assumed(reason)
    if ua and ua[0]:
        return f"We also took one {ua[0]} as one {ua[1]}; this decision settles both."
    return "No unit was stated either; this decision settles both."
