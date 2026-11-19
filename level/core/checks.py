"""Fixed checks run by code (PLAN §6): T3 arithmetic, T4 plausibility, T5 spec, T6 coverage.

Each check returns plain data saying what it found; callers decide which flag to open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from level.copy import number
from level.core.normalise import parse_number
from level.types import CellState, SpecDeviation

# ---------------------------------------------------------------------------
# T3 arithmetic
# ---------------------------------------------------------------------------


def amounts_agree(a: Decimal, b: Decimal, tolerance: Decimal = Decimal("0.005")) -> bool:
    """Equal within a relative tolerance (default 0.5%) or one rupee, whichever is larger."""
    allowed = max(abs(b) * tolerance, Decimal(1))
    return abs(a - b) <= allowed


@dataclass
class ArithmeticResult:
    passed: bool
    expected: Decimal | None
    stated: Decimal | None
    detail: str


def _dp(value: Decimal) -> int:
    """Decimals to show for a figure as written: 1200 -> 0, 4.5 -> 2."""
    return 0 if value == value.to_integral_value() else 2


def check_line_amount(qty: Decimal, rate: Decimal, stated_amount: Decimal) -> ArithmeticResult:
    """qty x rate must equal the amount the vendor wrote."""
    expected = qty * rate
    ok = amounts_agree(expected, stated_amount)
    detail = (f"{number(qty, _dp(qty))} x {number(rate, _dp(rate))} = "
              f"{number(expected, 2)}; vendor wrote {number(stated_amount, 2)}" if not ok
              else "qty x rate matches the stated amount")
    return ArithmeticResult(ok, expected, stated_amount, detail)


def check_total(amounts: list[Decimal], stated_total: Decimal) -> ArithmeticResult:
    """Line amounts must add up to the stated total."""
    expected = sum(amounts, Decimal(0))
    ok = amounts_agree(expected, stated_total)
    detail = (f"lines add up to {number(expected, 2)}; stated total is "
              f"{number(stated_total, 2)} (a difference of "
              f"{number(stated_total - expected, 2)})" if not ok
              else "lines add up to the stated total")
    return ArithmeticResult(ok, expected, stated_total, detail)


# ---------------------------------------------------------------------------
# T4 plausibility
# ---------------------------------------------------------------------------

UNIT_MISMATCH_RATIOS = (Decimal(10), Decimal(100), Decimal(1000))
MISMATCH_BAND = Decimal("0.35")  # ratio within +/-35% of 10, 100 or 1000 (log-ish band)
FAR_RATIO = Decimal(3)  # more than 3x away from the reference is implausible


@dataclass
class PlausibilityResult:
    passed: bool
    ratio: Decimal | None  # price / reference
    reference: Decimal | None
    reference_kind: str | None  # "peer median" or "last year"
    unit_mismatch: Decimal | None  # 10, 100 or 1000 when the ratio looks like a unit slip
    detail: str


def _mismatch_factor(ratio: Decimal) -> Decimal | None:
    """10, 100 or 1000 if the ratio (or its inverse) is close to one of them."""
    for r in (ratio, 1 / ratio):
        for factor in UNIT_MISMATCH_RATIOS:
            if abs(r - factor) <= factor * MISMATCH_BAND:
                return factor
    return None


UNIT_SLIP = "unit-basis mismatch"  # words in a T4 reason that mark a likely unit slip


def check_plausibility(
    price: Decimal, peer_prices: list[Decimal], last_price: Decimal | None
) -> PlausibilityResult:
    """Compare a price with the median of other vendors' prices, else last year's price."""
    peers = [p for p in peer_prices if p and p > 0]
    if peers:
        reference, kind = Decimal(median(peers)), "peer median"
    elif last_price and last_price > 0:
        reference, kind = last_price, "last year"
    else:
        return PlausibilityResult(True, None, None, None, None, "no reference price to compare")
    if price <= 0:
        return PlausibilityResult(False, None, reference, kind, None, "price is zero or negative")
    ratio = price / reference
    factor = _mismatch_factor(ratio)
    if factor is not None:
        direction = "higher" if ratio > 1 else "lower"
        return PlausibilityResult(
            False, ratio, reference, kind, factor,
            f"about {factor}x {direction} than the {kind} ({reference:.2f}): "
            f"likely a {UNIT_SLIP} (e.g. per {factor} vs per piece)")
    if ratio > FAR_RATIO or ratio < 1 / FAR_RATIO:
        return PlausibilityResult(False, ratio, reference, kind, None,
                                  f"{ratio:.2f}x the {kind} ({reference:.2f})")
    return PlausibilityResult(True, ratio, reference, kind, None,
                              f"{ratio:.2f}x the {kind}")


# ---------------------------------------------------------------------------
# T5 spec comparison
# ---------------------------------------------------------------------------


def _norm(value) -> str:
    return re.sub(r"\s+", "", str(value).strip().lower())


def _numbers(value) -> list[Decimal]:
    return [Decimal(n) for n in re.findall(r"\d+(?:\.\d+)?", str(value))]


def compare_spec(rfx_spec: dict, quoted_spec: dict) -> list[SpecDeviation]:
    """Attributes present in both that differ. Works on any attribute names.

    Numbers are compared as numbers (so '400x300x250' equals '400 x 300 x 250 mm'),
    everything else as normalised text. Attributes only on one side are not deviations.
    """
    deviations = []
    quoted = {_norm(k): v for k, v in (quoted_spec or {}).items()}
    for key, rfx_value in (rfx_spec or {}).items():
        q = quoted.get(_norm(key))
        if q is None or rfx_value is None or str(q).strip() == "":
            continue
        rfx_nums, q_nums = _numbers(rfx_value), _numbers(q)
        if rfx_nums and q_nums:
            same = rfx_nums == q_nums
        else:
            same = _norm(rfx_value) == _norm(q)
        if not same:
            deviations.append(SpecDeviation(attribute=str(key), rfx_value=str(rfx_value),
                                            quoted_value=str(q)))
    return deviations


# ---------------------------------------------------------------------------
# T6 coverage
# ---------------------------------------------------------------------------


def coverage(
    vendor_ids: list[int], line_ids: list[int], cells: dict[tuple[int, int], CellState]
) -> dict:
    """Every vendor x line must have exactly one state. Returns counts and any gaps."""
    missing = [(v, ln) for v in vendor_ids for ln in line_ids if (v, ln) not in cells]
    counts: dict[str, int] = {}
    for state in cells.values():
        counts[state.value] = counts.get(state.value, 0) + 1
    return {"complete": not missing, "missing": missing, "counts": counts,
            "expected": len(vendor_ids) * len(line_ids)}


def parse_amount(text: str | None) -> Decimal | None:
    """Convenience for callers holding amounts as written."""
    return parse_number(text)
