"""Turn a price as the vendor wrote it into INR per the RFx line's own unit.

Everything here is plain arithmetic on Decimal, with every step recorded as a
ConversionStep so the receipt can show the chain.

Units are handled with a small graph. Built-in edges cover units that mean the same
thing everywhere (1 kg = 1000 g, 1 dozen = 12 each). Each RFx line adds its own edges from
`conversion_factors`, written as "<unit>_per_<unit>": {"g_per_piece": "412"} means one
piece weighs 412 g. A price per kg becomes a price per piece by walking piece -> g -> kg.
Nothing here knows what is being bought.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from level.types import Basis, ConversionStep, QuantityBreak

# ---------------------------------------------------------------------------
# Parsing numbers as written
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?|\.\d+")
_SCALE_WORDS = {
    "k": Decimal(1000), "thousand": Decimal(1000), "lakh": Decimal(100000),
    "lac": Decimal(100000), "lakhs": Decimal(100000), "crore": Decimal(10000000),
    "cr": Decimal(10000000), "mn": Decimal(1000000), "million": Decimal(1000000),
}


def parse_number(text: str | None) -> Decimal | None:
    """First number in the text: '₹1,250.00/-' -> 1250.00, '50k' -> 50000. None if no number,
    or if the number has an unreadable character in it ('4?.50')."""
    if text is None:
        return None
    text = str(text).strip()
    match = _NUMBER.search(text)
    if not match:
        return None
    around = text[max(0, match.start() - 1):match.start()] + text[match.end():match.end() + 1]
    if "?" in around:  # '4?.50': a digit could not be read, so there is no number to use
        return None
    try:
        value = Decimal(match.group().replace(",", ""))
    except InvalidOperation:
        return None
    # A scale word right after the number: "1.5 lakh", "50k".
    rest = text[match.end():].strip().lower()
    word = re.match(r"[a-z]+", rest)
    if word and word.group() in _SCALE_WORDS:
        value *= _SCALE_WORDS[word.group()]
    return value


def parse_percent(text: str | None) -> Decimal | None:
    """'2%' or '2 percent' -> Decimal('0.02'). None if the text has no percentage."""
    if not text or not re.search(r"%|percent|pct", text, re.IGNORECASE):
        return None
    number = parse_number(text)
    return None if number is None else number / 100


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

_CURRENCY_WORDS = {
    "INR": ["inr", "₹", "rs", "rs.", "rupee", "rupees", "re"],
    "USD": ["usd", "us$", "$", "dollar", "dollars", "us dollar"],
    "EUR": ["eur", "€", "euro", "euros"],
    "GBP": ["gbp", "£", "pound", "pounds"],
    "CNY": ["cny", "rmb", "yuan"],
    "AED": ["aed", "dirham", "dirhams"],
}


def currency_code(text: str | None) -> str | None:
    """'Rs.' -> 'INR', 'US$' -> 'USD'. None when unknown or absent."""
    if not text:
        return None
    t = text.strip().lower()
    for code, words in _CURRENCY_WORDS.items():
        if t in words:
            return code
    for code, words in _CURRENCY_WORDS.items():  # symbol inside a longer string: "₹ 42"
        if any(w in t for w in words if not w.isalpha()):
            return code
    if re.fullmatch(r"[a-z]{3}", t):
        return t.upper()
    return None


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

# Words that all mean "one of the thing". Mapped to one internal name.
_COUNT_WORDS = {
    "each", "ea", "piece", "pieces", "pc", "pcs", "nos", "no", "number", "numbers",
    "unit", "units", "item", "items", "qty",
}
_ALIASES = {
    "kgs": "kg", "kilogram": "kg", "kilograms": "kg", "kilo": "kg", "kilos": "kg",
    "gram": "g", "grams": "g", "gm": "g", "gms": "g", "gr": "g",
    "tonne": "t", "tonnes": "t", "ton": "t", "tons": "t", "mt": "t",
    "lb": "lb", "lbs": "lb", "pound": "lb",
    "dozens": "dozen", "dz": "dozen", "doz": "dozen",
    "hundred": "hundred", "thousand": "thousand", "gross": "gross",
    "metre": "m", "meter": "m", "metres": "m", "meters": "m", "mtr": "m", "mtrs": "m",
    "centimetre": "cm", "millimetre": "mm", "feet": "ft", "foot": "ft",
    "litre": "l", "liter": "l", "litres": "l", "liters": "l", "ltr": "l",
    "hours": "hour", "hr": "hour", "hrs": "hour", "days": "day",
}
_BUILTIN_EDGES = [  # (a, b, f): one a = f of b
    ("kg", "g", Decimal(1000)), ("t", "kg", Decimal(1000)),
    ("lb", "g", Decimal("453.59237")),
    ("dozen", "each", Decimal(12)), ("gross", "each", Decimal(144)),
    ("hundred", "each", Decimal(100)), ("thousand", "each", Decimal(1000)),
    ("m", "cm", Decimal(100)), ("m", "mm", Decimal(1000)), ("ft", "mm", Decimal("304.8")),
    ("l", "ml", Decimal(1000)), ("day", "hour", Decimal(24)),
]
# Units with a known physical dimension. A quoted unit we have never heard of is treated
# as "probably the line's own unit" (an assumption, flagged); a known unit with no path
# (per kg, but the line has no weight) cannot be converted at all.
KNOWN_UNITS = {u for a, b, _ in _BUILTIN_EDGES for u in (a, b)}
# Item units that are several pieces by definition: 'per piece' cannot mean one of these.
_SEVERAL_PIECES = {"pair", "pairs"}


def unit_key(text: str | None) -> str | None:
    """Normalise a unit word: 'Pcs.' -> 'each', 'Kgs' -> 'kg', 'cartons' -> 'carton'."""
    if not text:
        return None
    t = re.sub(r"[^a-z]", "", text.strip().lower())
    if not t:
        return None
    if t in _COUNT_WORDS:
        return "each"
    if t in _ALIASES:
        return _ALIASES[t]
    if t in KNOWN_UNITS:
        return t
    if t.endswith("s") and len(t) > 2:  # plural of an unknown word: 'boxes' -> 'box'
        t = t[:-2] if t.endswith("xes") else t[:-1]
    return t


def _line_edges(factors: dict) -> list[tuple[str, str, Decimal]]:
    """Edges from conversion_factors: {'g_per_piece': '412'} -> (piece, g, 412)."""
    edges = []
    for key, raw in (factors or {}).items():
        if "_per_" not in key:
            continue  # other keys (notes, ref names) are ignored, not guessed at
        top, bottom = key.split("_per_", 1)
        value = parse_number(str(raw))
        a, b = unit_key(bottom), unit_key(top)
        if value and value > 0 and a and b:
            edges.append((a, b, value))
    return edges


def unit_path(
    from_unit: str, to_unit: str, factors: dict | None = None
) -> list[tuple[str, str, str, Decimal, str]] | None:
    """Shortest chain from a price per one unit to a price per another.

    Each hop is (from, to, 'multiply' or 'divide', factor, source). Factors are kept as
    written and applied by multiply or divide, so 30 per pack / 6 is exactly 5.
    None when no chain exists; an empty list when the units are the same.
    """
    graph: dict[str, list[tuple[str, str, Decimal, str]]] = {}

    def add(a: str, b: str, f: Decimal, source: str) -> None:
        # One a = f of b. A price per b becomes a price per a by multiplying by f
        # (one a holds f of b), and a price per a becomes per b by dividing by f.
        graph.setdefault(b, []).append((a, "multiply", f, source))
        graph.setdefault(a, []).append((b, "divide", f, source))

    for a, b, f in _BUILTIN_EDGES:
        add(a, b, f, "standard unit")
    for a, b, f in _line_edges(factors or {}):
        add(a, b, f, "line.conversion_factors")

    if from_unit == to_unit:
        return []
    seen = {from_unit}
    queue: deque[tuple[str, list]] = deque([(from_unit, [])])
    while queue:  # breadth-first search: the shortest chain of conversions
        unit, path = queue.popleft()
        for nxt, op, f, source in graph.get(unit, []):
            if nxt in seen:
                continue
            step = (unit, nxt, op, f, source)
            if nxt == to_unit:
                return path + [step]
            seen.add(nxt)
            queue.append((nxt, path + [step]))
    return None


def apply_step(value: Decimal, op: str, factor: Decimal) -> Decimal:
    return value * factor if op == "multiply" else value / factor


# ---------------------------------------------------------------------------
# The full normalisation of one price
# ---------------------------------------------------------------------------


@dataclass
class Normalised:
    """A price in INR per canonical unit, or the reason it could not be computed."""

    value: Decimal | None
    steps: list[ConversionStep] = field(default_factory=list)
    assumptions: dict[str, str] = field(default_factory=dict)  # key -> value used
    notes: list[str] = field(default_factory=list)  # plain-text remarks for the reason
    error: str | None = None


def _step(steps: list, label: str, op: str, value: Decimal, factor: Decimal,
          output: Decimal, source: str | None) -> Decimal:
    steps.append(ConversionStep(label=label, operation=op, input=value, factor=factor,
                                output=output, source=source))
    return output


_TAX_PHRASE = re.compile(r"\b(ex|excl\w*|incl\w*|inc|plus|with|without)[\s.-]*(of\s+)?"
                         r"(gst|tax|taxes|vat|igst|cgst)\b", re.IGNORECASE)


_PACK_OF = re.compile(r"\bof\s+(\d[\d,.]*)\s*([a-z]+)?", re.IGNORECASE)  # "box of 50 pair"
_COUNT_SIZES = {"dozen": Decimal(12), "gross": Decimal(144)}


def quoted_unit(basis: Basis | None, uom_text: str | None) -> tuple[str | None, Decimal]:
    """(unit, multiple) from what was written. 'per 1000 pcs' -> ('each', 1000)."""
    per = basis.per if basis else None
    multiple = parse_number(basis.multiple) if basis and basis.multiple else None
    # 'box of 50 pair': the vendor states what the pack holds, so the price is for 50 pairs.
    # Read that first, or the 50 is divided out once here and again by a box->pair factor.
    for written in (uom_text, per):
        pack = _PACK_OF.search(_TAX_PHRASE.sub(" ", written or ""))
        size = parse_number(pack.group(1)) if pack else None
        if size and pack.group(2):
            return unit_key(pack.group(2)), size
        if size:  # 'box of 50': 50 of the line's own item, by the vendor's own count
            return None, size
    text = _TAX_PHRASE.sub(" ", per or uom_text or "")  # "ex-GST" is a tax note, not a unit
    # A number inside the unit text itself: per='100 pcs' or uom='per 100'.
    inline = parse_number(text)
    if inline is not None and multiple is None:
        multiple = inline
    words = re.sub(r"[\d.,/]+", " ", text.lower()).replace("per", " ").split()
    for word, size in _COUNT_SIZES.items():  # 'per dozen sets' = 12 sets
        if word in words and len(words) > 1:
            words.remove(word)
            multiple = (multiple or 1) * size
    unit = unit_key(words[-1]) if words else None
    if unit is None and multiple is not None:
        unit = "each"  # 'per 100' with no word means 100 of the item
    if unit in ("hundred", "thousand"):  # 'per thousand' = 1000 each
        multiple = (multiple or 1) * (100 if unit == "hundred" else 1000)
        unit = "each"
    return unit, multiple or Decimal(1)


_TAX_INCL = re.compile(
    r"(incl(?:\.|usive|uding)?|inc\.|with)\s*(?:of\s*)?(gst|tax|taxes|vat|igst|cgst)",
    re.IGNORECASE,
)
_TAX_EXCL = re.compile(
    r"(excl(?:\.|usive|uding)?|ex\.?|plus|\+|without)\s*(?:of\s*)?(gst|tax|taxes|vat)",
    re.IGNORECASE,
)


def is_tax_inclusive(*texts: str | None) -> bool:
    """True when the vendor's own words say the price includes tax ('incl. GST')."""
    joined = " ".join(t for t in texts if t)
    return bool(_TAX_INCL.search(joined)) and not _TAX_EXCL.search(joined)


def normalise_price(
    raw_price: str | None,
    *,
    raw_currency: str | None,
    basis: Basis | None,
    uom_text: str | None,
    canonical_uom: str,
    conversion_factors: dict | None,
    fx_rates: dict[str, Decimal],
    base_currency: str = "INR",
    tax_inclusive: bool = False,
    tax_rate: Decimal | None = None,
    discounts: list[tuple[Decimal, str]] | None = None,
) -> Normalised:
    """As-quoted price -> base currency per canonical unit, ex-tax, after discounts.

    fx_rates: {"USD": Decimal("83.2")} meaning 1 USD = 83.2 base currency.
    discounts: (fraction, description) pairs to apply, e.g. (Decimal("0.02"), "2% term").
    """
    result = Normalised(value=None)
    price = parse_number(raw_price)
    if price is None:
        unreadable = raw_price and any(ch.isdigit() for ch in str(raw_price))
        result.error = (f"price could not be read: '{raw_price}'" if unreadable
                        else "no price stated")
        return result
    steps = result.steps
    value = price

    # 1. Unit basis -> the line's canonical unit.
    unit, multiple = quoted_unit(basis, uom_text)
    target = unit_key(canonical_uom) or "each"
    if multiple != 1:
        value = _step(steps, f"per {multiple} {unit} -> per 1 {unit}",
                      f"divide by {multiple}", value, multiple, value / multiple, "as quoted")
    if unit is None:
        unit = target
        result.notes.append(f"no unit stated; taken as per {canonical_uom}")
        result.assumptions["unit"] = f"no unit stated = 1 {canonical_uom}"
    path = unit_path(unit, target, conversion_factors)
    if path is None and unit == "each" and target not in KNOWN_UNITS | _SEVERAL_PIECES:
        # A generic count word ('per pc', 'per 1000 nos') on a line counted in its own item
        # unit ('set', 'roll'): one piece is taken as one of the line's items. An assumption,
        # shown on the cell (Inferred), and the plausibility check still applies.
        result.assumptions["unit"] = f"1 {unit} = 1 {canonical_uom}"
        result.notes.append(f"'{unit}' taken as one {canonical_uom}")
        path = []
    if path is None and unit not in KNOWN_UNITS:
        # A unit word we do not know (e.g. 'box', 'roll'): assume it is the line's own unit,
        # say so, and let the plausibility check catch a wrong assumption.
        result.assumptions["unit"] = f"1 {unit} = 1 {canonical_uom}"
        result.notes.append(f"'{unit}' assumed to mean one {canonical_uom}")
        path = []
    if path is None:
        result.error = (f"cannot convert per {unit} to per {canonical_uom}: "
                        f"the line has no factor linking them")
        return result
    for a, b, op, f, source in path:
        value = _step(steps, f"per {a} -> per {b}", f"{op} by {_fmt(f)}",
                      value, f, apply_step(value, op, f), source)
        if source == "line.conversion_factors":
            result.assumptions[f"factor.{a}->{b}"] = f"{op} by {_fmt(f)}"

    # 2. Currency -> base currency at the pinned rate.
    code = currency_code(raw_currency) or currency_code(raw_price)
    if code is None:
        code = base_currency
        result.notes.append(f"no currency stated; taken as {base_currency}")
        result.assumptions["currency"] = f"no currency stated = {base_currency}"
    if code != base_currency:
        rate = fx_rates.get(code)
        if rate is None:
            result.error = f"no exchange rate for {code}"
            return result
        value = _step(steps, f"{code} -> {base_currency}", f"multiply by {rate}",
                      value, rate, value * rate, f"fx.{code}{base_currency}")
        result.assumptions[f"fx.{code}{base_currency}"] = str(rate)

    # 3. Tax-inclusive -> ex-tax.
    if tax_inclusive:
        if tax_rate is None:
            result.error = "price includes tax but no tax rate is set"
            return result
        divisor = 1 + tax_rate
        value = _step(steps, "tax-inclusive -> ex-tax", f"divide by {divisor}",
                      value, divisor, value / divisor, "gst_rate")
        result.assumptions["gst_rate"] = str(tax_rate)

    # 4. Discounts that apply without conditions (or that the caller chose to apply).
    for fraction, description in discounts or []:
        keep = 1 - fraction
        value = _step(steps, f"discount: {description}", f"multiply by {keep}",
                      value, keep, value * keep, "vendor term")

    result.value = value
    return result


def _fmt(d: Decimal) -> str:
    """Short readable form of a factor: 0.412 not 0.4120000000000000000000000001."""
    q = d.quantize(Decimal("0.000001")).normalize()
    return format(q, "f")


def round_money(value: Decimal | None, places: str = "0.0001") -> Decimal | None:
    """Round for storage and display. Arithmetic stays unrounded until here."""
    return None if value is None else value.quantize(Decimal(places))


# ---------------------------------------------------------------------------
# Volume slabs
# ---------------------------------------------------------------------------


def slab_fits(qb: QuantityBreak | None, annual_qty: Decimal | None) -> bool:
    """Does this slab cover the line's annual quantity? No slab = covers everything."""
    if qb is None or annual_qty is None:
        return True
    low = parse_number(qb.min_qty)
    high = parse_number(qb.max_qty)
    if low is None and high is None:
        # Bounds only in the text: 'above 50,000' / 'upto 10000'.
        number = parse_number(qb.text)
        text = qb.text.lower()
        if number is None:
            return True
        if re.search(r"above|over|more than|>|\+|min|from", text):
            low = number
        elif re.search(r"up ?to|below|under|less than|<|max|till", text):
            high = number
        else:
            return True
    if low is not None and annual_qty < low:
        return False
    return not (high is not None and annual_qty > high)


def select_slab(breaks: list[QuantityBreak | None], annual_qty: Decimal | None) -> int | None:
    """Index of the slab that matches the annual quantity, or None if none fits.

    When several fit (overlapping slabs), the one with the highest lower bound wins.
    """
    fitting = [i for i, qb in enumerate(breaks) if slab_fits(qb, annual_qty)]
    if not fitting:
        return None

    def lower(i: int) -> Decimal:
        qb = breaks[i]
        if qb is None:
            return Decimal(-1)
        return parse_number(qb.min_qty) or parse_number(qb.text) or Decimal(0)

    return max(fitting, key=lower)
