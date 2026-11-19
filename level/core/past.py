"""Award value of a past event: what the awarded lines were worth per year (WP13).

Pure function, no database. Unknown stays unknown: a line without a price or a quantity is
left out and counted, never valued at zero, and no priced line at all gives None.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def award_value(awarded: list[dict]) -> tuple[Decimal | None, int]:
    """Sum of unit_price x annual_qty over awarded lines.

    Each item: {"unit_price": "12.50", "annual_qty": "20000"} (decimal strings).
    Returns (total, lines left out because price or quantity is missing).
    """
    total = None
    left_out = 0
    for line in awarded:
        price, qty = _dec(line.get("unit_price")), _dec(line.get("annual_qty"))
        if price is None or qty is None:
            left_out += 1
            continue
        total = (total or Decimal("0")) + price * qty
    return total, left_out
