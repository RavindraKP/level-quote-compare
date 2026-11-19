"""Build the comparison grid: one Cell per vendor x line, from claims, terms, assumptions,
history and flags. Nothing else writes the `cell` table.

Two halves:
- `load_snapshot(rfx_id)` reads everything a grid needs from SQLite into plain objects.
- `compute_cells(snapshot, ...)` is a pure function of that snapshot. Scenarios call it with
  what-if overrides (FX rate, conditional discounts) without touching stored data.

State rules, in this order (PLAN §6): no price -> Not quoted; open conflict flag -> Conflict;
open review flag or failed evidence -> Needs review; an assumption used -> Inferred;
otherwise Confirmed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal

import sqlalchemy as sa

from level import db
from level.core import landed as landed_mod
from level.core.checks import UNIT_SLIP
from level.core.normalise import (
    Normalised,
    is_tax_inclusive,
    normalise_price,
    parse_number,
    parse_percent,
    round_money,
    select_slab,
)
from level.types import (
    FLAG_CELL_EFFECT,
    Basis,
    Cell,
    CellState,
    ClosedBy,
    Flag,
    FlagType,
    QuantityBreak,
)

DEFAULT_REQUESTED_PAYMENT_DAYS = Decimal(60)
DEFAULT_COST_OF_CAPITAL = Decimal("0.12")
SCOPE_PRIORITY = {"line": 0, "rule": 1, "reference": 2}  # specific beats general


@dataclass
class LineInfo:
    id: int
    line_no: int
    description: str
    canonical_uom: str
    annual_qty: Decimal | None
    factors: dict
    spec: dict
    last_price: Decimal | None
    plant: str | None = None  # delivery place; a freight term may differ by place


@dataclass
class VendorInfo:
    id: int
    name: str
    status: str  # replied / invited / no_response


@dataclass
class ClaimInfo:
    id: int
    vendor_id: int
    line_ids: list[int]
    scope: str
    item_text: str | None
    raw_price: str | None
    raw_currency: str | None
    uom_text: str | None
    basis: Basis | None
    quantity_break: QuantityBreak | None
    evidence_locator: str | None
    evidence_quote: str | None
    evidence_ok: bool | None
    recency: str  # when the message carrying this claim arrived; later supersedes earlier
    vendor_confirmed_lines: list[int] = field(default_factory=list)  # confirmed via reply link
    # stated by the vendor in the reply-link chat, not yet confirmed as converted
    vendor_stated_lines: list[int] = field(default_factory=list)


@dataclass
class TermInfo:
    id: int
    vendor_id: int
    type: str
    text: str | None
    value: str | None
    conditions: str | None
    is_conditional: bool
    evidence_locator: str | None
    recency: str


@dataclass
class Snapshot:
    rfx_id: int
    base_currency: str
    requested_payment_days: Decimal
    lines: list[LineInfo]
    vendors: list[VendorInfo]
    claims: list[ClaimInfo]
    terms: list[TermInfo]
    flags: list[Flag]
    assumptions: dict[str, str]
    history: dict[tuple[int, int], dict] = field(default_factory=dict)  # (vendor, line) -> row
    gating: dict[int, list[str]] = field(default_factory=dict)  # vendor -> gating verdicts


# ---------------------------------------------------------------------------
# Pure computation
# ---------------------------------------------------------------------------


def fx_rates(assumptions: dict[str, str], base: str, override: dict | None = None) -> dict:
    """{'USD': Decimal('83.2')} from assumption keys like 'fx.USDINR'."""
    rates = {}
    for key, value in assumptions.items():
        m = re.fullmatch(r"fx\.([A-Z]{3})" + base, key)
        if m and parse_number(value) is not None:
            rates[m.group(1)] = parse_number(value)
    for key, value in (override or {}).items():  # 'USDINR' or 'USD'
        rates[key[:3].upper()] = Decimal(str(value))
    return rates


def _page(locator: str | None) -> str | None:
    m = re.search(r"page\s*(\d+)", locator or "", re.IGNORECASE)
    return m.group(1) if m else None


def _latest(items: list, key=lambda x: x.recency):
    return max(items, key=lambda x: (key(x), x.id)) if items else None


def pick_claim(claims: list[ClaimInfo], line: LineInfo) -> tuple[ClaimInfo | None, str]:
    """The claim that sets this vendor's price for this line, and why it was chosen."""
    if not claims:
        return None, ""
    newest = max(c.recency for c in claims)
    current = [c for c in claims if c.recency == newest]  # a later message supersedes
    best_scope = min(SCOPE_PRIORITY.get(c.scope, 9) for c in current)
    current = [c for c in current if SCOPE_PRIORITY.get(c.scope, 9) == best_scope]
    note = "superseded an earlier quote" if len({c.recency for c in claims}) > 1 else ""
    if len(current) == 1:
        return current[0], note
    with_slabs = [c for c in current if c.quantity_break is not None]
    if with_slabs:
        idx = select_slab([c.quantity_break for c in with_slabs], line.annual_qty)
        if idx is None:
            return None, "no volume slab covers the annual quantity"
        chosen = with_slabs[idx]
        return chosen, f"slab '{chosen.quantity_break.text}' matches annual qty {line.annual_qty}"
    return _latest(current, key=lambda c: c.recency), note


def _flags_for(snapshot: Snapshot, vendor_id: int, line_id: int, claim_id: int | None):
    """Flags that touch this cell: on its claim, or on this vendor x line directly."""
    out = []
    for f in snapshot.flags:
        on_claim = claim_id is not None and f.target_kind == "claim" and f.target_id == claim_id
        on_cell = f.vendor_id == vendor_id and f.rfx_line_id == line_id
        if on_claim or on_cell:
            out.append(f)
    return out


def _resolution(flags: list[Flag]) -> tuple[str | None, str | None, ClosedBy | None]:
    """A buyer or vendor resolution: ('not_quoted'|'corrected'|'corrected_per_unit', price, who).

    'corrected' is a price as the vendor wrote it (converted as usual); 'corrected_per_unit'
    is the Review screen's figure, already in the base currency per the line's unit.
    """
    for f in flags:
        if f.state != "closed" or not f.resolution:
            continue
        decision = _review_decision(f.resolution)
        if decision is not None:  # the Review screen's JSON form
            if decision.get("action") == "not_quoted":
                return "not_quoted", None, f.closed_by
            if decision.get("action") == "edit_value" and decision.get("value"):
                return "corrected_per_unit", str(decision["value"]), f.closed_by
            continue
        if f.resolution == "not_quoted":
            return "not_quoted", None, f.closed_by
        if f.resolution.startswith("corrected:"):
            return "corrected", f.resolution.split(":", 1)[1].strip(), f.closed_by
    return None, None, None


def _review_decision(resolution: str) -> dict | None:
    """flag.resolution as written by the Review screen: {"action", "value", "note"} JSON."""
    if not resolution.startswith("{"):
        return None
    try:
        decision = json.loads(resolution)
    except ValueError:
        return None
    return decision if isinstance(decision, dict) else None


def _discounts(terms: list[TermInfo], apply_conditional: bool) -> list[tuple[Decimal, str]]:
    out = []
    for t in terms:
        if t.type != "discount":
            continue
        pct = parse_percent(t.value) or parse_percent(t.text)
        if pct is None or (t.is_conditional and not apply_conditional):
            continue
        label = f"{pct * 100}%" + (" (conditional, applied by choice)" if t.is_conditional else "")
        out.append((pct, label))
    return out


def _tax_inclusive(claim: ClaimInfo, terms: list[TermInfo]) -> bool:
    if is_tax_inclusive(claim.uom_text, claim.item_text, claim.evidence_quote):
        return True
    for t in terms:  # a tax term covers the claim when it is document-wide or on the same page
        if t.type == "tax" and is_tax_inclusive(t.text, t.value):
            page = _page(t.evidence_locator)
            if page is None or page == _page(claim.evidence_locator):
                return True
    return False


_TAX_WORDS = re.compile(r"^\(?\s*(ex|excl|excluding|incl|including|inclusive|exclusive)"
                        r"[\s.-]*(of\s+)?(gst|tax|taxes)\b.*$", re.IGNORECASE)
_HAS_CURRENCY = re.compile(r"[^\d\s.,?-]")  # a symbol or word next to the digits ("Rs. 40")


def _as_quoted(claim: ClaimInfo) -> str:
    """The price as the vendor wrote it, e.g. 'Rs 42 / kg'; their words when no number.

    The vendor's own text often already carries the currency ("Rs. 40.50") or a slash
    ("/ kg"), so neither is added twice. A tax note in the unit field ("ex-GST") goes in
    brackets instead of being shown as a unit.
    """
    if claim.raw_price is None:
        return claim.item_text or ""
    basis = claim.basis
    from_basis = (f"{basis.multiple + ' ' if basis.multiple else ''}{basis.per}"
                  if basis and basis.per else "")
    unit, tax = (claim.uom_text or "").strip(), ""
    if _TAX_WORDS.match(unit):
        unit, tax = from_basis, unit.strip("() ")
    unit = unit or from_basis
    if "/" in unit:  # "Rate / 1000 Nos" or "/ kg": the unit is what follows the last slash
        unit = unit.rsplit("/", 1)[1]
    unit = re.sub(r"^\s*per\s+", "", unit.strip(), flags=re.IGNORECASE).strip()
    price = claim.raw_price.strip()
    currency = "" if _HAS_CURRENCY.search(price) else (claim.raw_currency or "")
    parts = [currency, price, f"/ {unit}" if unit else "", f"({tax})" if tax else ""]
    return " ".join(p for p in parts if p)


def in_totals(cell: Cell) -> bool:
    """Whether a cell's price may be added into a total (H4).

    A price the plausibility check calls a likely unit slip ("Rs 328 per box" against peers
    near Rs 3) is shown, but left out of every total until it is settled, the same way an
    unknown price is: adding it at face value would overstate the total a hundredfold.
    """
    return not (cell.state == CellState.NEEDS_REVIEW and UNIT_SLIP in cell.reason)


def compute_cells(
    snapshot: Snapshot,
    *,
    fx_override: dict | None = None,
    apply_conditional_discounts: bool = False,
    include_payment_terms: bool = False,
) -> list[Cell]:
    """One Cell per vendor x line. Pure: reads only the snapshot.

    include_payment_terms=False leaves the carrying cost out of landed (price + freight only).
    """
    base = snapshot.base_currency
    rates = fx_rates(snapshot.assumptions, base, fx_override)
    tax_rate = parse_number(snapshot.assumptions.get("gst_rate"))
    coc = parse_number(snapshot.assumptions.get("cost_of_capital")) or DEFAULT_COST_OF_CAPITAL
    if not include_payment_terms:
        coc = Decimal(0)  # a zero cost of capital adds nothing for any payment gap
    cells = []
    for vendor in snapshot.vendors:
        v_claims = [c for c in snapshot.claims if c.vendor_id == vendor.id]
        v_terms = [t for t in snapshot.terms if t.vendor_id == vendor.id]
        for line in snapshot.lines:
            cells.append(_one_cell(snapshot, vendor, line, v_claims, v_terms, rates, tax_rate,
                                   coc, apply_conditional_discounts))
    return cells


def _one_cell(snapshot, vendor, line, v_claims, v_terms, rates, tax_rate, coc,
              apply_conditional) -> Cell:
    base_kwargs = {"rfx_id": snapshot.rfx_id, "vendor_id": vendor.id, "rfx_line_id": line.id}
    if vendor.status != "replied" and not v_claims:
        reason = "no response" if vendor.status == "no_response" else "awaiting reply"
        return Cell(state=CellState.AWAITING, reason=reason, **base_kwargs)

    candidates = [c for c in v_claims if line.id in c.line_ids]
    claim, pick_note = pick_claim(candidates, line)
    flags = _flags_for(snapshot, vendor.id, line.id, claim.id if claim else None)
    flag_ids = [f.id for f in flags if f.id is not None]
    if claim is None:
        reason = pick_note or "vendor did not quote this line"
        return Cell(state=CellState.NOT_QUOTED, reason=reason, flag_ids=flag_ids, **base_kwargs)

    resolution, corrected_price, resolved_by = _resolution(flags)
    if resolution == "not_quoted":
        return Cell(state=CellState.NOT_QUOTED, reason=f"marked not quoted by {resolved_by}",
                    claim_id=claim.id, flag_ids=flag_ids, confirmed_by=resolved_by, **base_kwargs)

    raw_price, currency = claim.raw_price, claim.raw_currency
    basis, uom = claim.basis, claim.uom_text
    extra_assumptions: dict[str, str] = {}
    if claim.scope == "reference":
        hist = snapshot.history.get((vendor.id, line.id))
        if hist is None:
            return Cell(state=CellState.NOT_QUOTED, claim_id=claim.id, flag_ids=flag_ids,
                        reason="vendor referred to last year's price, but has no record for "
                               "this line", **base_kwargs)
        raw_price, currency = hist["unit_price"], hist.get("currency") or snapshot.base_currency
        basis, uom = None, hist.get("uom") or line.canonical_uom
        kind = _HISTORY_SOURCE.get(hist.get("source"), "price history")
        extra_assumptions[f"history.{hist.get('period') or 'last'}"] = (
            f"{raw_price} {currency} per {uom} ({kind})")
    if corrected_price is not None:
        raw_price = corrected_price
    if resolution == "corrected_per_unit":  # Review screen: already base currency per line unit
        currency, basis, uom = snapshot.base_currency, None, line.canonical_uom

    norm: Normalised = normalise_price(
        raw_price, raw_currency=currency, basis=basis, uom_text=uom,
        canonical_uom=line.canonical_uom, conversion_factors=line.factors, fx_rates=rates,
        base_currency=snapshot.base_currency,
        tax_inclusive=(claim.scope != "reference" and resolution != "corrected_per_unit"
                       and _tax_inclusive(claim, v_terms)),
        tax_rate=tax_rate,
        discounts=[] if resolution == "corrected_per_unit" else _discounts(v_terms,
                                                                          apply_conditional),
    )
    as_quoted = _as_quoted(claim)
    price_assumptions = {**extra_assumptions, **norm.assumptions}  # these make a cell Inferred
    assumptions = dict(price_assumptions)  # plus landed-only ones (cost of capital), shown only
    if norm.value is None and raw_price is None:
        unreadable = [f for f in flags if f.state == "open" and f.type == FlagType.READ_MISMATCH]
        if unreadable:  # the vendor did quote; we could not read the digits. Unknown, not "no".
            seen = [c for f in unreadable for c in f.candidates] or ["illegible"]
            return Cell(state=CellState.NEEDS_REVIEW, claim_id=claim.id, as_quoted=as_quoted,
                        reason=f"price could not be read with certainty: {' or '.join(seen)}",
                        flag_ids=flag_ids, **base_kwargs)
        return Cell(state=CellState.NOT_QUOTED, reason="no price stated", claim_id=claim.id,
                    as_quoted="", flag_ids=flag_ids, **base_kwargs)  # no price: show nothing

    landed_value, steps = None, list(norm.steps)
    notes = list(norm.notes) + ([pick_note] if pick_note else [])
    if norm.value is not None:
        # Every freight term, newest first: a later answer for one place replaces the
        # freight there only, and other places keep the earlier term (QA pass 3 B1).
        freight_terms = sorted([t for t in v_terms if t.type == "freight"],
                               key=lambda t: (t.recency, t.id), reverse=True)
        pay_term = _latest([t for t in v_terms if t.type == "payment"])
        places = sorted({ln.plant for ln in snapshot.lines if ln.plant})
        freight = landed_mod.freight_from_terms([(t.text, t.value) for t in freight_terms],
                                                line.plant, places)
        pay_days = (landed_mod.parse_days(pay_term.value or pay_term.text) if pay_term else None)
        land = landed_mod.landed_price(
            norm.value, freight=freight, canonical_uom=line.canonical_uom,
            conversion_factors=line.factors, fx_rates=rates, payment_days=pay_days,
            requested_days=snapshot.requested_payment_days, cost_of_capital=coc,
            base_currency=snapshot.base_currency)
        landed_value = land.value
        steps += land.steps
        assumptions.update(land.assumptions)
        if land.unknown_reason:
            notes.append(f"landed unknown: {land.unknown_reason}")
        notes += land.notes

    state, reason, confirmed_by = _state(norm, flags, claim, price_assumptions, notes,
                                         corrected_price is not None,
                                         line.id in claim.vendor_confirmed_lines,
                                         line.id in claim.vendor_stated_lines)
    return Cell(
        state=state, reason=reason, claim_id=claim.id, as_quoted=as_quoted,
        unit_price_inr=round_money(norm.value), landed_price_inr=round_money(landed_value),
        conversion_steps=steps, assumptions=assumptions, flag_ids=flag_ids,
        confirmed_by=confirmed_by, **base_kwargs,
    )


def _state(norm, flags, claim, assumptions, notes, corrected, vendor_confirmed=False,
           vendor_stated=False):
    """Apply the PLAN §6 state order. Returns (state, reason, confirmed_by)."""
    open_flags = [f for f in flags if f.state == "open"]
    effects = {f.id: FLAG_CELL_EFFECT.get(FlagType(f.type)) for f in open_flags}
    conflict = [f for f in open_flags if effects[f.id] == CellState.CONFLICT]
    review = [f for f in open_flags if effects[f.id] == CellState.NEEDS_REVIEW]
    closed_types = {f.type for f in flags if f.state == "closed"}
    evidence_bad = (claim.evidence_ok is not True and not corrected
                    and not ({FlagType.EVIDENCE, FlagType.READ_MISMATCH} & closed_types))

    if norm.value is None:
        return CellState.NEEDS_REVIEW, norm.error or "price could not be normalised", None
    if conflict:
        return CellState.CONFLICT, "; ".join(f.reason for f in conflict), None
    if review or evidence_bad:
        reasons = [f.reason for f in review]
        if evidence_bad and not any(f.type == FlagType.EVIDENCE for f in review):
            reasons.append("evidence not verified in the source" if claim.evidence_ok is False
                           else "evidence not checked yet")
        return CellState.NEEDS_REVIEW, "; ".join(reasons), None
    closers = {f.closed_by for f in flags if f.state == "closed" and f.closed_by}
    who = (ClosedBy.VENDOR if ClosedBy.VENDOR in closers or vendor_confirmed or vendor_stated
           else
           ClosedBy.BUYER if ClosedBy.BUYER in closers or corrected else ClosedBy.CHECKS)
    inferred_flags = [f for f in open_flags if effects[f.id] == CellState.INFERRED]
    if vendor_confirmed and not inferred_flags:
        # The vendor confirmed this price, as shown, through the reply link (PLAN §6, T15):
        # it is theirs now. The assumptions behind it stay on the receipt (UX audit H1).
        used = ", ".join(f"{k} = {v}" for k, v in assumptions.items())
        bits = ["confirmed by vendor", f"uses {used}" if used else ""] + notes
        return CellState.CONFIRMED, "; ".join(b for b in bits if b), ClosedBy.VENDOR
    if assumptions or inferred_flags:
        used = ", ".join(f"{k} = {v}" for k, v in assumptions.items())
        bits = [f"uses {used}" if used else ""] + [f.reason for f in inferred_flags] + notes
        return CellState.INFERRED, "; ".join(b for b in bits if b), who
    reason = {ClosedBy.CHECKS: "passed all checks", ClosedBy.BUYER: "confirmed by buyer",
              ClosedBy.VENDOR: "confirmed by vendor"}[who]
    return CellState.CONFIRMED, "; ".join([reason] + notes), who


# ---------------------------------------------------------------------------
# Loading from and writing to SQLite
# ---------------------------------------------------------------------------


def _dec(text) -> Decimal | None:
    return parse_number(text) if text not in (None, "") else None


# How a history record is described in a cell's assumptions (buyer-facing words).
_HISTORY_SOURCE = {"contract": "last year's contract price", "quote": "their quote last year"}


def own_record(rows: list[dict]) -> dict:
    """One vendor's record for one line: its contract price first, else its own last quote.

    `rows` are that vendor's price_history rows for that line, oldest first. Another vendor's
    rows are never passed in, so a reference can only resolve from the vendor's own history.
    """
    contracts = [r for r in rows if r.get("source") == "contract"]
    return (contracts or rows)[-1]  # newest of the preferred kind


def load_snapshot(rfx_id: int, conn: sa.Connection | None = None) -> Snapshot:
    """Read everything the grid needs for one RFx."""
    if conn is None:
        with db.connect() as c:
            return load_snapshot(rfx_id, c)
    r = conn.execute(sa.select(db.rfx).where(db.rfx.c.id == rfx_id)).mappings().one()
    terms_json = r["terms"] or {}
    days = terms_json.get("payment_days") or terms_json.get("payment_days_requested") or ""
    requested = _dec(str(days)) or DEFAULT_REQUESTED_PAYMENT_DAYS  # co-pilot / seed spellings

    lines = [
        LineInfo(id=x["id"], line_no=x["line_no"], description=x["description"],
                 canonical_uom=x["canonical_uom"], annual_qty=_dec(x["annual_qty"]),
                 factors=x["conversion_factors"] or {}, spec=x["spec"] or {},
                 last_price=_dec(x["last_price"]), plant=x["plant"])
        for x in conn.execute(sa.select(db.rfx_line).where(db.rfx_line.c.rfx_id == rfx_id)
                              .order_by(db.rfx_line.c.line_no)).mappings()
    ]
    inv = conn.execute(
        sa.select(db.vendor.c.id, db.vendor.c.name, db.invitation.c.status)
        .join(db.invitation, db.invitation.c.vendor_id == db.vendor.c.id)
        .where(db.invitation.c.rfx_id == rfx_id).order_by(db.vendor.c.id)
    ).mappings().all()
    vendors = [VendorInfo(id=x["id"], name=x["name"], status=x["status"]) for x in inv]

    # When did the message carrying each claim / term arrive? Later supersedes earlier.
    recency_sql = sa.text("""
        SELECT er.id AS run_id,
               COALESCE(m.received_at, m.created_at, er.created_at) AS recency
        FROM extraction_run er
        LEFT JOIN attachment a ON a.id = er.attachment_id
        LEFT JOIN message m ON m.id = COALESCE(a.message_id, er.message_id)
    """)
    recency = {row.run_id: row.recency for row in conn.execute(recency_sql)}

    mapping: dict[int, list[int]] = {}
    confirmed: dict[int, list[int]] = {}  # claim -> lines the vendor confirmed (reply link)
    stated: dict[int, list[int]] = {}  # claim -> lines the vendor priced in the reply link
    for row in conn.execute(sa.select(db.claim_line.c.claim_id, db.claim_line.c.rfx_line_id,
                                      db.claim_line.c.status)
                            .where(db.claim_line.c.status != "rejected")):
        mapping.setdefault(row.claim_id, []).append(row.rfx_line_id)
        if row.status == "vendor_confirmed":
            confirmed.setdefault(row.claim_id, []).append(row.rfx_line_id)
        if row.status == "vendor_stated":
            stated.setdefault(row.claim_id, []).append(row.rfx_line_id)

    claims = []
    for x in conn.execute(sa.select(db.quote_claim).where(db.quote_claim.c.rfx_id == rfx_id)
                          ).mappings():
        claims.append(ClaimInfo(
            id=x["id"], vendor_id=x["vendor_id"], line_ids=mapping.get(x["id"], []),
            scope=x["scope"], item_text=x["vendor_item_text"], raw_price=x["raw_price"],
            raw_currency=x["raw_currency"], uom_text=x["raw_uom_text"],
            basis=Basis(**x["basis"]) if x["basis"] else None,
            quantity_break=QuantityBreak(**x["quantity_break"]) if x["quantity_break"] else None,
            evidence_locator=x["evidence_locator"], evidence_quote=x["evidence_quote"],
            evidence_ok=x["evidence_ok"],
            recency=recency.get(x["run_id"]) or x["created_at"] or "",
            vendor_confirmed_lines=confirmed.get(x["id"], []),
            vendor_stated_lines=stated.get(x["id"], []),
        ))
    terms = [
        TermInfo(id=x["id"], vendor_id=x["vendor_id"], type=x["type"], text=x["text"],
                 value=x["value"], conditions=x["conditions"],
                 is_conditional=bool(x["is_conditional"]), evidence_locator=x["evidence_locator"],
                 recency=recency.get(x["run_id"]) or x["created_at"] or "")
        for x in conn.execute(sa.select(db.vendor_term).where(db.vendor_term.c.rfx_id == rfx_id)
                              ).mappings()
    ]
    flags = [
        Flag(id=x["id"], rfx_id=x["rfx_id"], vendor_id=x["vendor_id"],
             target_kind=x["target_kind"], target_id=x["target_id"],
             rfx_line_id=x["rfx_line_id"], type=x["type"], severity=x["severity"],
             reason=x["reason"], suggested_action=x["suggested_action"],
             candidates=x["candidates"] or [], state=x["state"], closed_by=x["closed_by"],
             resolution=x["resolution"])
        for x in conn.execute(sa.select(db.flag).where(db.flag.c.rfx_id == rfx_id)).mappings()
    ]
    assumptions = {x.key: x.value for x in conn.execute(sa.select(db.assumption))}
    line_ids = {ln.id for ln in lines}
    records: dict[tuple[int, int], list[dict]] = {}
    for x in conn.execute(sa.select(db.price_history).order_by(db.price_history.c.id)).mappings():
        if x["rfx_line_id"] in line_ids:
            records.setdefault((x["vendor_id"], x["rfx_line_id"]), []).append(dict(x))
    history = {key: own_record(rows) for key, rows in records.items()}

    gating: dict[int, list[str]] = {v.id: [] for v in vendors}
    gating_qs = [q.id for q in conn.execute(
        sa.select(db.question.c.id).where(db.question.c.rfx_id == rfx_id,
                                          db.question.c.is_gating.is_(True)))]
    answers = {(a.vendor_id, a.question_id): a.verdict for a in conn.execute(
        sa.select(db.answer.c.vendor_id, db.answer.c.question_id, db.answer.c.verdict)
        .where(db.answer.c.rfx_id == rfx_id))}
    for v in vendors:
        gating[v.id] = [answers.get((v.id, q)) or "unclear" for q in gating_qs]

    return Snapshot(rfx_id=rfx_id, base_currency=r["base_currency"] or "INR",
                    requested_payment_days=requested, lines=lines, vendors=vendors,
                    claims=claims, terms=terms, flags=flags, assumptions=assumptions,
                    history=history, gating=gating)


def _cell_row(c: Cell) -> dict:
    return {
        "rfx_id": c.rfx_id, "vendor_id": c.vendor_id, "rfx_line_id": c.rfx_line_id,
        "state": c.state.value, "reason": c.reason, "claim_id": c.claim_id,
        "as_quoted": c.as_quoted,
        "unit_price_inr": None if c.unit_price_inr is None else str(c.unit_price_inr),
        "landed_price_inr": None if c.landed_price_inr is None else str(c.landed_price_inr),
        "conversion_steps": [s.model_dump(mode="json") for s in c.conversion_steps],
        "assumptions": c.assumptions, "flag_ids": c.flag_ids,
        "confirmed_by": c.confirmed_by.value if c.confirmed_by else None,
        "updated_at": db.now_iso(),
    }


def rebuild_cells(rfx_id: int) -> list[Cell]:
    """Recompute and replace every cell of one RFx. Call after any change to its inputs."""
    from level.core import views  # local import: views reads a .sql file

    snapshot = load_snapshot(rfx_id)
    cells = compute_cells(snapshot)
    with db.begin() as conn:
        conn.execute(db.cell.delete().where(db.cell.c.rfx_id == rfx_id))
        if cells:
            conn.execute(db.cell.insert(), [_cell_row(c) for c in cells])
        views.install(conn)
    return cells
