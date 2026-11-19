"""Review queue arithmetic: money at stake, queue order, price context, same-cause groups.

Pure functions only (no database, no Streamlit). The Review page and its data layer call
these so that every number a buyer sees on that page is computed here, in tested code.
Nothing here decides anything; it only orders, groups and summarises what the checks found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median


def stake(annual_qty: Decimal | None, unit_price: Decimal | None) -> Decimal:
    """Annual value at stake on one line: quantity x the price we have (0 if either is unknown)."""
    if annual_qty is None or unit_price is None:
        return Decimal(0)
    return annual_qty * unit_price


OUTLIER_RATIO = Decimal(10)  # a price this many times the median is likely another unit


def review_stake(annual_qty: Decimal | None, unit_price: Decimal | None,
                 vs_median: Decimal | None, peer_median: Decimal | None) -> Decimal:
    """Money at stake for ordering the Review queue. A price 10 or more times (or a tenth
    or less of) the other quotes' median is likely a different unit, so its stake is taken
    at the median instead of the suspect price; otherwise one unit slip would put its
    vendor at the top of the queue (UX audit M9). Used for ordering only, never a total."""
    if vs_median is not None and peer_median is not None and (
            vs_median >= OUTLIER_RATIO or vs_median <= 1 / OUTLIER_RATIO):
        return stake(annual_qty, peer_median)
    return stake(annual_qty, unit_price)


def median_price(prices: list[Decimal]) -> Decimal | None:
    """Median of the prices given, or None when there are none."""
    return Decimal(median(prices)) if prices else None


def ratio(price: Decimal | None, reference: Decimal | None) -> Decimal | None:
    """price / reference, rounded to one decimal for display ('3.2' times). None if unknown."""
    if price is None or not reference:
        return None
    return (price / reference).quantize(Decimal("0.1"))


@dataclass
class QueueItem:
    """One issue as the queue sees it: who it is for, how much rides on it, and its order keys."""

    key: str  # stable across rebuilds (flag ids of grid-level issues change on every rebuild)
    vendor_id: int | None
    stake: Decimal
    severity_rank: int  # 0 = high
    flag_id: int


def order_queue(items: list[QueueItem]) -> list[QueueItem]:
    """Vendors with most money at stake first; within a vendor, most money first, then
    priority, then oldest. Line-less issues (no money attached) end each vendor's list."""
    per_vendor: dict[int | None, Decimal] = {}
    for it in items:
        per_vendor[it.vendor_id] = per_vendor.get(it.vendor_id, Decimal(0)) + it.stake
    return sorted(items, key=lambda it: (-per_vendor[it.vendor_id], it.vendor_id or 0,
                                         -it.stake, it.severity_rank, it.flag_id))


def vendor_totals(items: list[QueueItem]) -> dict[int | None, Decimal]:
    """Vendor id -> money at stake across the issues given."""
    out: dict[int | None, Decimal] = {}
    for it in items:
        out[it.vendor_id] = out.get(it.vendor_id, Decimal(0)) + it.stake
    return out


def position(keys: list[str], current: str | None) -> tuple[int, int]:
    """(1-based place of `current` in `keys`, total). (0, total) when it is not there."""
    return (keys.index(current) + 1 if current in keys else 0), len(keys)


def step(keys: list[str], current: str | None, direction: int) -> str | None:
    """The key before (-1) or after (+1) `current`, or None at either end."""
    if current not in keys:
        return keys[0] if keys else None
    i = keys.index(current) + direction
    return keys[i] if 0 <= i < len(keys) else None


def next_open(ordered: list[str], open_keys: set[str], current: str | None) -> str | None:
    """The first open issue after `current` in queue order, wrapping to the top; None if done."""
    start = ordered.index(current) + 1 if current in ordered else 0
    for key in ordered[start:] + ordered[:start]:
        if key in open_keys and key != current:
            return key
    return None


# --- Price context: what a buyer needs to judge one price ----------------------------------


@dataclass
class Record:
    """One price from last year's records (price_history)."""

    vendor_id: int
    price: Decimal
    source: str  # "contract" (what the buyer paid) or "quote" (a bid that did not win)
    period: str  # e.g. "FY25"
    uom: str | None = None
    date: str | None = None  # ISO date when the record has one (the past event's award date)


@dataclass
class PeerPrice:
    vendor_id: int
    price: Decimal
    state: str  # the peer cell's state; shown so an unverified peer price reads as such


@dataclass
class PriceContext:
    """Buyer-only context for one vendor's price on one line. Never shown to a vendor."""

    own_record: Record | None  # this vendor's own record for the line last year
    peers: list[PeerPrice] = field(default_factory=list)  # other vendors, this year
    peer_median: Decimal | None = None  # median of the other vendors' prices this year
    last_contract: Record | None = None  # last year's contract price, from any supplier
    this_price: Decimal | None = None  # the price we currently hold for this vendor
    vs_median: Decimal | None = None  # this_price / peer_median, one decimal


def own_record(rows: list[Record]) -> Record | None:
    """A vendor's record for a line: its contract price first, else its own last quote
    (the same preference the cell rebuild uses). Rows are oldest first."""
    if not rows:
        return None
    contracts = [r for r in rows if r.source == "contract"]
    return (contracts or rows)[-1]


def price_context(vendor_id: int, this_price: Decimal | None, history: list[Record],
                  peers: list[PeerPrice]) -> PriceContext:
    """Assemble the context for one vendor x line from last year's records (every vendor's,
    for this line) and this year's prices from the other vendors."""
    mine = own_record([r for r in history if r.vendor_id == vendor_id])
    contracts = [r for r in history if r.source == "contract"]
    others = [p for p in peers if p.vendor_id != vendor_id]
    mid = median_price([p.price for p in others])
    return PriceContext(own_record=mine, peers=sorted(others, key=lambda p: p.price),
                        peer_median=mid, last_contract=contracts[-1] if contracts else None,
                        this_price=this_price, vs_median=ratio(this_price, mid))


# --- Same-cause groups: one confirmed action for many issues --------------------------------


@dataclass
class BulkCandidate:
    """An open issue that could be accepted together with others of the same cause."""

    key: str
    flag_id: int
    vendor_id: int | None
    cause: tuple  # (issue type, what the accept would do, the shared root-cause words)
    line_no: int | None
    value: Decimal | None  # the price that accepting keeps


def same_cause_groups(items: list[BulkCandidate], minimum: int = 2) -> dict[tuple, list]:
    """(vendor id, cause) -> the open issues that share it, in line order, when there are at
    least `minimum`. Only issues with a line and a known price can be accepted together."""
    groups: dict[tuple, list[BulkCandidate]] = {}
    for it in items:
        if it.line_no is None or it.value is None:
            continue
        groups.setdefault((it.vendor_id, it.cause), []).append(it)
    return {k: sorted(v, key=lambda it: it.line_no) for k, v in groups.items()
            if len(v) >= minimum}
