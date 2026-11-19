"""Landed price: ex-works price + freight + the cost of paying earlier than the RFx asked.

landed = ex_works + freight per unit
         + ex_works x cost_of_capital x (requested_days - vendor_days) / 365

A vendor asking for 30-day payment when the RFx asks for 60 costs the buyer 30 days of
money, so their price is effectively higher. Longer terms than asked lower it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from level.core.normalise import (
    apply_step,
    currency_code,
    parse_number,
    parse_percent,
    unit_key,
    unit_path,
)
from level.types import ConversionStep

DAYS_PER_YEAR = Decimal(365)


@dataclass
class Freight:
    """Freight as stated by the vendor, parsed by code."""

    kind: str  # included / per_unit / percent / per_trip / unknown
    amount: Decimal | None = None
    unit: str | None = None  # for per_unit: the unit the amount is per
    currency: str | None = None
    text: str = ""
    reason: str | None = None  # for unknown: why, in words the buyer can act on


_INCLUDED = re.compile(
    r"\b(incl(uded|usive|\.)?|free|delivered|for destination|f\.?o\.?r\.?|door delivery|"
    r"no charge|borne by us)\b", re.IGNORECASE,
)
_EXTRA = re.compile(r"\b(extra|additional|ex[- ]?works|exw|to pay|(at|on) actuals?|buyer)\b",
                    re.IGNORECASE)


def _names(clause: str, place: str) -> bool:
    return bool(re.search(rf"\b{re.escape(place)}\b", clause, re.IGNORECASE))


names_place = _names  # public name: does this text name this place (whole words)?


def freight_for_place(text: str, place: str | None, places: list[str] | None = None) -> str:
    """The part of a freight term that applies to one delivery place.

    "EUR 4 per unit to Lyon and Turin; Oslo at cost." -> for Oslo, "Oslo at cost."; for
    Lyon, the first clause. `places` is every delivery place in the RFx: clauses that name
    one of the others do not apply here, so for a place the term does not name only its
    general clauses are left (possibly none, which means unknown). A term that names no
    place applies everywhere.
    """
    if not place or not text:
        return text
    clauses = [c.strip() for c in re.split(r"[;\n]|(?<=\.)\s+", text) if c.strip()]
    named = [c for c in clauses if _names(c, place)]
    if named:
        return " ".join(named)
    others = [p for p in (places or []) if p and p.lower() != place.lower()]
    general = [c for c in clauses if not any(_names(c, p) for p in others)]
    return " ".join(general)


def freight_for_line(text: str | None, value: str | None, place: str | None,
                     places: list[str] | None = None) -> Freight:
    """The freight that applies to one line, given its delivery place and all RFx places."""
    full = text or ""
    part = freight_for_place(full, place, places)
    if full.strip() and not part.strip():
        return Freight("unknown", text=full, reason=(
            f"the freight term names other delivery places, not {place} ('{full}')"))
    return parse_freight(part, value if part == full else None, place)


def covers_place(text: str | None, place: str | None, places: list[str] | None = None) -> bool:
    """True if a freight term says anything about this place (by name, or in general)."""
    return bool(freight_for_place(text or "", place, places).strip())


def freight_from_terms(terms: list[tuple[str | None, str | None]], place: str | None,
                       places: list[str] | None = None) -> Freight | None:
    """The freight for one line from all of a vendor's freight terms, newest first.

    Each term is (text, value). The newest term that covers the line's place wins, so an
    answer naming one place replaces the freight there only; other places keep the earlier
    term that covers them. If no term covers the place, the newest one explains why it is
    unknown. No term at all gives None (freight not stated).
    """
    if not terms:
        return None
    for text, value in terms:
        if covers_place(text, place, places) or not (text or "").strip():
            return freight_for_line(text, value, place, places)
    return freight_for_line(terms[0][0], terms[0][1], place, places)


def answer_places(value: str, unit: str | None, message: str,
                  places: list[str]) -> list[str] | None:
    """The delivery places a vendor's freight answer is about. [] means every place.

    Places named in the answer itself count first; otherwise the places named in the
    sentence(s) of the vendor's message that carry the answer's figure (or its words).
    None when that sentence gives several figures for several places: code cannot tell
    which figure goes where, so it must not guess.
    """
    named = [p for p in places if _names(f"{value} {unit or ''}", p)]
    if named:
        return named
    figure = re.search(r"\d[\d,]*(?:\.\d+)?", value)
    if figure:  # the figure as a whole number in the sentence: "2.40" is not in "12.40"
        pattern = rf"(?<![\d.]){re.escape(figure.group().replace(',', ''))}(?!\d)"
    else:
        pattern = re.escape(value.strip().rstrip(".").lower())
    sentences = [s for s in re.split(r"[;\n]|(?<=[.!?])\s+", message or "") if s.strip()]
    holding = [s for s in sentences if re.search(pattern, s.replace(",", "").lower())]
    found = [p for p in places if any(_names(s, p) for s in holding)]
    figures = {n for s in holding for n in re.findall(r"\d[\d,]*(?:\.\d+)?", s)}
    if len(found) > 1 and len(figures) > 1:
        return None
    return found


def _per(unit: str | None) -> str:
    """'piece', 'per piece', '/piece' or 'per per piece' -> 'per piece'."""
    word = re.sub(r"^(?:\s*(?:per\b|/))+\s*", "", (unit or "").strip(), flags=re.IGNORECASE)
    return f"per {word or 'unit'}"


def freight_answer_text(value: str, unit: str | None, currency: str,
                        places: list[str] | None = None) -> str:
    """A freight answer as a term the freight rules can read, keeping the vendor's figure.

    "Rs 1.50", "per piece", INR -> "freight Rs 1.50 per piece". With places, the term ends
    with them ("freight EUR 4 per unit, to Lyon"), so it applies only there. They go last so
    that a number or "per" in a place name is never read as the amount or the unit.
    """
    # One clause only: a full stop or ";" inside would split it into a term for every place.
    value = re.sub(r"\s+", " ", re.sub(r"[;\n]+|\.(?=\s|$)", " ", value)).strip()
    if value.lower() in {"included", "freight included", "delivered"}:
        body = "freight included in the price (delivered)"
    elif any(ch.isdigit() for ch in value):
        has_currency = _symbol_currency(value) or re.search(r"\b[A-Z]{3}\b", value)
        figure = value if has_currency else f"{currency} {value}"
        # The unit may already be in the value ("1.50 per piece"); never write it twice.
        body = (f"freight {figure}" if re.search(r"(?:\bper\b|/)\s*[a-zA-Z]", figure)
                else f"freight {figure} {_per(unit)}")
    else:  # e.g. "extra at actuals": no amount stated
        words = re.sub(r"^freight\s*:?\s*", "", value, flags=re.IGNORECASE)
        body = f"freight {words}"
    return f"{body}, to {' and '.join(places)}" if places else body


_LIST_MARKER = re.compile(r"^\s*(\(?\d{1,2}[.)]|\(?[a-z][.)])\s+", re.IGNORECASE)  # "1. ", "a) "
_DISTANCE = re.compile(r"\b\d[\d,.]*\s*(km|kms|kilomet(er|re)s?|miles?|mi)\b", re.IGNORECASE)


def parse_freight(text: str | None, value: str | None = None,
                  place: str | None = None) -> Freight:
    """Read a freight term. Anything we cannot pin down is 'unknown', never zero.

    A distance ("free within 40 km") is a condition, not a charge. Code does not know how
    far a delivery place is, so a term with a distance condition is unknown unless it names
    `place` (the line's delivery place). A list number ("1. Rates are delivered") is not an
    amount either; both are set aside before looking for an amount. The full wording stays
    in `text` for the receipt and the question to the vendor.
    """
    original = (text or "").strip()
    if value and value.strip().lower() not in original.lower():  # value not already in text
        original = f"{value.strip()} {original}".strip()
    if _DISTANCE.search(original) and not (place and _names(original, place)):
        where = f" to {place}" if place else ""
        return Freight("unknown", text=original, reason=(
            f"freight depends on distance and the distance{where} is not known "
            f"('{original}')"))
    joined = _DISTANCE.sub(" ", _LIST_MARKER.sub("", original)).strip()
    if not joined:
        return Freight("unknown", text=original)
    pct = parse_percent(joined)
    if pct is not None:
        return Freight("percent", amount=pct, text=original)
    value = _DISTANCE.sub(" ", _LIST_MARKER.sub("", value)).strip() if value else None
    amount = parse_number(value) if value else None
    if amount is None:
        amount = parse_number(joined)
    if amount is not None and amount > 0:
        if re.search(r"\b(trip|truck|load|vehicle|consignment|shipment|lot)\b", joined, re.I):
            return Freight("per_trip", amount=amount, text=original)
        unit_match = re.search(r"(?:per|/)\s*([a-zA-Z]+)", joined)
        unit = unit_key(unit_match.group(1)) if unit_match else None
        return Freight("per_unit", amount=amount, unit=unit,
                       currency=currency_code(joined) or _symbol_currency(joined), text=original)
    if _EXTRA.search(joined):
        return Freight("unknown", text=original)
    if _INCLUDED.search(joined):
        return Freight("included", amount=Decimal(0), text=original)
    return Freight("unknown", text=original)


def _symbol_currency(text: str) -> str | None:
    for token in re.findall(r"[₹$€£]|rs\.?|inr|usd", text, re.IGNORECASE):
        code = currency_code(token)
        if code:
            return code
    return None


def parse_days(text: str | None) -> Decimal | None:
    """'60 days credit' -> 60. 'advance' / 'against delivery' -> 0."""
    if not text:
        return None
    if re.search(r"\b(advance|against delivery|cash on delivery|cod|immediate)\b", text, re.I):
        return Decimal(0)
    return parse_number(text)


@dataclass
class Landed:
    value: Decimal | None
    steps: list[ConversionStep] = field(default_factory=list)
    assumptions: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    unknown_reason: str | None = None


def landed_price(
    ex_works: Decimal,
    *,
    freight: Freight | None,
    canonical_uom: str,
    conversion_factors: dict | None,
    fx_rates: dict[str, Decimal],
    payment_days: Decimal | None,
    requested_days: Decimal,
    cost_of_capital: Decimal,
    base_currency: str = "INR",
) -> Landed:
    """Ex-works price per canonical unit -> landed price per canonical unit."""
    out = Landed(value=None)
    value = ex_works

    # Freight.
    if freight is None or freight.kind == "unknown":
        out.unknown_reason = (freight.reason if freight and freight.reason else
                              "freight amount not stated" + (
                                  f" ('{freight.text}')" if freight and freight.text else ""))
        return out
    if freight.kind == "per_trip":
        out.unknown_reason = f"freight is per trip ('{freight.text}'); pieces per trip not known"
        return out
    if freight.kind == "percent":
        add = ex_works * freight.amount
        out.steps.append(ConversionStep(
            label="freight", operation=f"add {freight.amount * 100}% of ex-works",
            input=value, factor=freight.amount, output=value + add, source="vendor term"))
        value += add
    elif freight.kind == "per_unit":
        per_unit = freight.amount
        code = freight.currency or base_currency
        if code != base_currency:
            rate = fx_rates.get(code)
            if rate is None:
                out.unknown_reason = f"freight in {code} and no exchange rate"
                return out
            per_unit *= rate
            out.assumptions[f"fx.{code}{base_currency}"] = str(rate)
        target = unit_key(canonical_uom) or "each"
        path = unit_path(freight.unit or target, target, conversion_factors)
        if path is None:
            out.unknown_reason = (f"freight is per {freight.unit}; no factor converts it to "
                                  f"per {canonical_uom}")
            return out
        for _a, _b, op, f, _src in path:
            per_unit = apply_step(per_unit, op, f)
        out.steps.append(ConversionStep(
            label="freight", operation=f"add {per_unit} per {canonical_uom}", input=value,
            factor=per_unit, output=value + per_unit, source="vendor term"))
        value += per_unit
    # kind == "included": nothing to add

    # Payment-terms carrying cost.
    if payment_days is None:
        out.notes.append(f"payment terms not stated; taken as the requested {requested_days} days")
        payment_days = requested_days
        out.assumptions["payment_days"] = f"not stated = {requested_days}"
    gap = requested_days - payment_days
    if gap != 0 and cost_of_capital:
        fraction = cost_of_capital * gap / DAYS_PER_YEAR
        add = ex_works * fraction
        out.steps.append(ConversionStep(
            label=f"payment {payment_days} days vs {requested_days} requested",
            operation=f"add ex-works x {cost_of_capital} x {gap}/365", input=value,
            factor=fraction, output=value + add, source="cost_of_capital"))
        out.assumptions["cost_of_capital"] = str(cost_of_capital)
        value += add
    out.value = value
    return out
