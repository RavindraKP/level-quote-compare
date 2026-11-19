"""The RFx draft: plain database functions used by both the co-pilot's tools and the page.

The draft in SQLite is the source of truth. Chat edits and direct edits both go through these
functions, which change only the fields they are given, so neither overwrites the other.
Category-neutral: a line's spec and conversion factors are whatever attributes it carries.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa

from level import copy, db
from level.agents.vendor_copilot.link import link_url
from level.core import past
from level.types import ScenarioResult

LINE_FIELDS = ("description", "canonical_uom", "annual_qty", "plant", "spec", "conversion_factors")
TERM_KEYS_NEEDED = ("price_basis", "currency", "payment_days", "delivery")
# "valid_on" (a date) is what the requirement check reads; "is_yes" and "valid_on_date" are
# accepted from older drafts and stored as "== yes" / "valid_on <award date>".
PASS_OPS = ("<=", "<", ">=", ">", "==", "is_yes", "valid_on", "valid_on_date")


def _audit(conn, actor: str, action: str, rfx_id: int, after) -> None:
    conn.execute(
        db.audit_event.insert().values(
            actor=actor,
            action=action,
            target_kind="rfx",
            target_id=rfx_id,
            after=after,
            at=db.now_iso(),
        )
    )


# --- The event ---------------------------------------------------------------------------


def current_draft(sandbox_id: int | None = None) -> dict | None:
    """The newest real draft. With sandbox_id, that sandbox while it is still a draft: the
    RFx page shows a sandbox only to the visitor working on it."""
    if sandbox_id is not None:
        where = [db.rfx.c.id == sandbox_id, db.rfx.c.is_sandbox]
    else:
        where = [sa.not_(db.rfx.c.is_sandbox)]
    with db.connect() as conn:
        row = (
            conn.execute(
                sa.select(db.rfx).where(db.rfx.c.status == "draft", *where)
                .order_by(db.rfx.c.id.desc())
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


_REF = re.compile(r"^(?P<head>.*?)(?P<num>\d+)$")  # 'SF/PKG/FY26/014' -> 'SF/PKG/FY26/', '014'


def next_ref(conn) -> str | None:
    """The buyer's own numbering, continued: the newest event numbered 'SF/PKG/FY26/014'
    makes the next draft 'SF/PKG/FY26/015'. None if no event has a number (UX audit M14)."""
    refs = [(t or {}).get("ref") for t in conn.execute(  # sandboxes have their own numbers
        sa.select(db.rfx.c.terms).where(sa.not_(db.rfx.c.is_sandbox))
        .order_by(db.rfx.c.id.desc())).scalars()]
    used = {r for r in refs if r}
    for ref in refs:
        m = _REF.match(ref or "")
        if m:
            n, width = int(m["num"]), len(m["num"])
            while f"{m['head']}{n:0{width}d}" in used:  # never reuse a number
                n += 1
            return f"{m['head']}{n:0{width}d}"
    return None


def new_draft(title: str = "New RFx", category: str | None = None) -> int:
    with db.begin() as conn:
        ref = next_ref(conn)
        rfx_id = conn.execute(
            db.rfx.insert().values(title=title, category=category, status="draft",
                                   terms={"ref": ref} if ref else {})
        ).inserted_primary_key[0]
        _audit(conn, "buyer", "rfx.created", rfx_id, {"title": title})
    return rfx_id


def set_header(rfx_id: int, title: str, category: str | None, actor: str = "buyer") -> None:
    with db.begin() as conn:
        conn.execute(
            db.rfx.update().where(db.rfx.c.id == rfx_id).values(title=title, category=category)
        )
        _audit(conn, actor, "rfx.header", rfx_id, {"title": title, "category": category})


def snapshot(rfx_id: int) -> dict:
    """Everything in the draft, as plain data (what the co-pilot sees each turn)."""
    with db.connect() as conn:
        rfx = dict(conn.execute(sa.select(db.rfx).where(db.rfx.c.id == rfx_id)).mappings().one())
        lines = [
            dict(r)
            for r in conn.execute(
                sa.select(db.rfx_line)
                .where(db.rfx_line.c.rfx_id == rfx_id)
                .order_by(db.rfx_line.c.line_no)
            ).mappings()
        ]
        questions = [
            dict(r)
            for r in conn.execute(
                sa.select(db.question)
                .where(db.question.c.rfx_id == rfx_id)
                .order_by(db.question.c.q_no)
            ).mappings()
        ]
        invited = [
            r["name"]
            for r in conn.execute(
                sa.select(db.vendor.c.name)
                .join(db.invitation, db.invitation.c.vendor_id == db.vendor.c.id)
                .where(db.invitation.c.rfx_id == rfx_id)
            ).mappings()
        ]
    keep = (
        "line_no",
        "description",
        "canonical_uom",
        "annual_qty",
        "plant",
        "spec",
        "conversion_factors",
    )
    return {
        "title": rfx["title"],
        "category": rfx["category"],
        "status": rfx["status"],
        "terms": rfx["terms"] or {},
        "lines": [{k: line[k] for k in keep} for line in lines],
        "questions": [
            {k: q[k] for k in ("q_no", "section", "text", "answer_type", "is_gating", "pass_rule")}
            for q in questions
        ],
        "invited_vendors": invited,
    }


# --- Lines -------------------------------------------------------------------------------


def _clean_qty(value) -> str | None:
    """Quantities are stored as decimal strings; '20,000' -> '20000'. Bad input is rejected."""
    if value in (None, ""):
        return None
    try:
        return str(Decimal(str(value).replace(",", "").strip()))
    except InvalidOperation as exc:
        raise ValueError(f"annual quantity must be a number, got {value!r}") from exc


def upsert_lines(rfx_id: int, lines: list[dict], actor: str = "buyer") -> list[int]:
    """Add or change lines by line_no. Only fields present and not None are changed."""
    changed = []
    with db.begin() as conn:
        existing = {
            r["line_no"]: r
            for r in conn.execute(
                sa.select(db.rfx_line).where(db.rfx_line.c.rfx_id == rfx_id)
            ).mappings()
        }
        next_no = max(existing, default=0) + 1
        for line in lines:
            line_no = line.get("line_no") or next_no
            next_no = max(next_no, line_no + 1)
            values = {k: line[k] for k in LINE_FIELDS if line.get(k) is not None}
            if "annual_qty" in values:
                values["annual_qty"] = _clean_qty(values["annual_qty"])
            if line_no in existing:
                if values:
                    conn.execute(
                        db.rfx_line.update()
                        .where(db.rfx_line.c.id == existing[line_no]["id"])
                        .values(**values)
                    )
            else:
                if not values.get("description"):
                    raise ValueError(f"new line {line_no} needs a description")
                conn.execute(
                    db.rfx_line.insert().values(
                        rfx_id=rfx_id,
                        line_no=line_no,
                        canonical_uom=values.pop("canonical_uom", ""),
                        spec=values.pop("spec", {}),
                        conversion_factors=values.pop("conversion_factors", {}),
                        **values,
                    )
                )
            changed.append(line_no)
        _audit(conn, actor, "rfx.lines.upsert", rfx_id, {"line_nos": changed})
    return changed


def remove_lines(rfx_id: int, line_nos: list[int], actor: str = "buyer") -> int:
    with db.begin() as conn:
        n = conn.execute(
            db.rfx_line.delete().where(
                db.rfx_line.c.rfx_id == rfx_id, db.rfx_line.c.line_no.in_(line_nos)
            )
        ).rowcount
        _audit(conn, actor, "rfx.lines.remove", rfx_id, {"line_nos": line_nos})
    return n


# --- Past events (WP13) -------------------------------------------------------------------

# Event bookkeeping kept in terms that belongs to that event only, never copied into a repeat.
EVENT_KEYS = ("ref", "title", "period", "issued", "due", "award_date")
DEFAULT_TITLE = "New RFx"
# Older events name some terms differently; a copy uses the names the completeness check
# and the email read (UX audit H10). old name -> name used now.
TERM_ALIASES = {"basis": "price_basis", "payment_days_requested": "payment_days"}


def standard_terms(terms: dict | None, currency: str | None = None) -> dict:
    """Terms under the names used now: 'basis' -> 'price_basis', 'payment_days_requested' ->
    'payment_days'; delivery from the plants list and currency from the event when missing.
    Nothing is invented: a term that was never stated stays missing."""
    out = {}
    for key, value in (terms or {}).items():
        new = TERM_ALIASES.get(key, key)
        if value not in (None, "", []) and new not in out:
            out[new] = str(value) if isinstance(value, int | float) else value
    if not out.get("delivery") and out.get("plants"):
        plants = out["plants"]
        out["delivery"] = "To " + (", ".join(plants) if isinstance(plants, list) else plants)
    if not out.get("currency") and currency:
        out["currency"] = currency
    return out


def history_sources(rfx_id: int) -> list[dict]:
    """Earlier events with lines, newest first: what a repeat of an earlier event can copy from."""
    with db.connect() as conn:
        rows = (
            conn.execute(
                sa.select(db.rfx.c.id, db.rfx.c.title, sa.func.count(db.rfx_line.c.id).label("n"))
                .join(db.rfx_line, db.rfx_line.c.rfx_id == db.rfx.c.id)
                .where(db.rfx.c.id != rfx_id, db.rfx.c.status != "draft",
                       sa.not_(db.rfx.c.is_sandbox))  # a sandbox is never "last year"
                .group_by(db.rfx.c.id)
                # finished (awarded) events first: "last year" is the last awarded event,
                # not this year's event still collecting quotes (UX audit H10)
                .order_by((db.rfx.c.status == "awarded").desc(), db.rfx.c.id.desc())
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def record_only_ids() -> set[int]:
    """Events loaded as a record of an award made outside Level. They have no replies to work
    on, so they appear under Past events only, not in the event picker."""
    with db.connect() as conn:
        return set(conn.execute(sa.select(db.award_line.c.rfx_id).distinct()).scalars())


def _awards(conn, rfx_id: int) -> dict[int, dict]:
    """Awarded supplier and price per line id: the stored record, else the award option the
    buyer chose in Level. Empty if the event was not awarded."""
    rows = conn.execute(
        sa.select(db.award_line.c.rfx_line_id, db.award_line.c.vendor_id,
                  db.award_line.c.unit_price).where(db.award_line.c.rfx_id == rfx_id)
    ).mappings().all()
    if rows:
        return {r["rfx_line_id"]: {"vendor_id": r["vendor_id"], "unit_price": r["unit_price"]}
                for r in rows}
    result = conn.execute(
        sa.select(db.scenario.c.result).where(
            db.scenario.c.rfx_id == rfx_id, db.scenario.c.is_award.is_(True))
    ).scalar()
    if not result:
        return {}
    chosen = ScenarioResult.model_validate(result)
    return {a.rfx_line_id: {"vendor_id": a.vendor_id,
                            "unit_price": None if a.unit_price_inr is None
                            else str(a.unit_price_inr)}
            for a in chosen.allocation if a.vendor_id is not None}


def _award_date(conn, rfx: dict) -> str | None:
    if rfx["status"] != "awarded":
        return None
    at = conn.execute(
        sa.select(sa.func.max(db.audit_event.c.at)).where(
            db.audit_event.c.action == "rfx.awarded",
            db.audit_event.c.target_kind == "scenario",
            db.audit_event.c.target_id.in_(
                sa.select(db.scenario.c.id).where(db.scenario.c.rfx_id == rfx["id"])))
    ).scalar()
    return at or (rfx["terms"] or {}).get("award_date")


def _year(rfx: dict) -> str:
    """The event's own period ('FY25') if it states one, else the period in its number
    ('SF/PKG/FY26/014' -> 'FY26'), else the year it was issued."""
    terms = rfx["terms"] or {}
    in_ref = re.search(r"\bFY\d{2,4}\b", str(terms.get("ref") or ""))
    return (terms.get("period") or (in_ref.group(0) if in_ref else None)
            or (rfx["issued_at"] or rfx["created_at"] or "")[:4])


def past_events(exclude_rfx_id: int | None = None) -> list[dict]:
    """Every finished (awarded) event, newest first, with its award summary. Events still
    collecting quotes are not past events (UX audit H10).

    The award value is computed by level/core/past.py from stored prices, never by a model.
    """
    with db.connect() as conn:
        events = [dict(r) for r in conn.execute(
            sa.select(db.rfx).where(db.rfx.c.status == "awarded", sa.not_(db.rfx.c.is_sandbox))
            .order_by(db.rfx.c.id.desc())
        ).mappings()]
        names = dict(conn.execute(sa.select(db.vendor.c.id, db.vendor.c.name)).all())
        out = []
        for rfx in events:
            if rfx["id"] == exclude_rfx_id:
                continue
            qty = dict(conn.execute(
                sa.select(db.rfx_line.c.id, db.rfx_line.c.annual_qty).where(
                    db.rfx_line.c.rfx_id == rfx["id"])).all())
            awards = _awards(conn, rfx["id"])
            invited = conn.execute(
                sa.select(sa.func.count()).select_from(db.invitation).where(
                    db.invitation.c.rfx_id == rfx["id"])).scalar()
            value, _ = past.award_value(
                [{"unit_price": a["unit_price"], "annual_qty": qty.get(line_id)}
                 for line_id, a in awards.items()])
            # suppliers by number of lines won, most first
            won = sorted({a["vendor_id"] for a in awards.values()},
                         key=lambda v: -sum(a["vendor_id"] == v for a in awards.values()))
            out.append({
                "id": rfx["id"],
                "number": copy.event_number(rfx),
                "title": rfx["title"],
                "year": _year(rfx),
                "status": rfx["status"],
                "invited": invited,
                "suppliers": [names[v] for v in won],
                "award_value": value,
                "award_date": _award_date(conn, rfx),
                "lines": len(qty),
            })
    return out


def past_event(rfx_id: int) -> dict:
    """One past event, read-only: lines with who was awarded each, terms and questionnaire."""
    snap = snapshot(rfx_id)
    with db.connect() as conn:
        ids = dict(conn.execute(
            sa.select(db.rfx_line.c.line_no, db.rfx_line.c.id).where(
                db.rfx_line.c.rfx_id == rfx_id)).all())
        awards = _awards(conn, rfx_id)
        names = dict(conn.execute(sa.select(db.vendor.c.id, db.vendor.c.name)).all())
    for line in snap["lines"]:
        a = awards.get(ids[line["line_no"]])
        line["awarded_to"] = names.get(a["vendor_id"]) if a else None
        line["awarded_price"] = a["unit_price"] if a else None
    snap["terms"] = {k: v for k, v in standard_terms(snap["terms"]).items()
                     if k not in EVENT_KEYS}
    return snap


def import_from_history(
    rfx_id: int, source_rfx_id: int | None = None, actor: str = "buyer"
) -> dict:
    """Start this draft from an earlier event (default: the newest one sent to vendors).

    Copies lines (number, spec, unit, quantity, plant, conversion factors), questionnaire,
    terms and invited vendors. What the draft already has wins: same line or question number,
    same term. The source event is only read, never changed. Each copied line keeps, as a
    buyer-only reference, the price and supplier it was awarded at in the source event (or,
    if that event was never awarded, the reference it carried itself).
    """
    sources = history_sources(rfx_id)
    if source_rfx_id is None:
        if not sources:
            return {"imported": 0, "message": "No earlier event with lines was found."}
        source_rfx_id = sources[0]["id"]
    if source_rfx_id == rfx_id:
        raise ValueError("an event cannot be started from itself")
    src = snapshot(source_rfx_id)
    with db.connect() as conn:
        src_rows = {r["line_no"]: r for r in conn.execute(
            sa.select(db.rfx_line).where(db.rfx_line.c.rfx_id == source_rfx_id)).mappings()}
        awards = _awards(conn, source_rfx_id)
        source_rfx = dict(conn.execute(
            sa.select(db.rfx).where(db.rfx.c.id == source_rfx_id)).mappings().one())
        have_lines = set(conn.execute(
            sa.select(db.rfx_line.c.line_no).where(db.rfx_line.c.rfx_id == rfx_id)).scalars())
        have_qs = set(conn.execute(
            sa.select(db.question.c.q_no).where(db.question.c.rfx_id == rfx_id)).scalars())
        target = dict(conn.execute(
            sa.select(db.rfx).where(db.rfx.c.id == rfx_id)).mappings().one())
    new_lines = [r for no, r in sorted(src_rows.items()) if no not in have_lines]
    new_qs = [q for q in src["questions"] if q["q_no"] not in have_qs]
    terms = dict(target["terms"] or {})
    copied = standard_terms(src["terms"], source_rfx["base_currency"])
    added_terms = {k: v for k, v in copied.items() if k not in EVENT_KEYS and k not in terms}
    with db.begin() as conn:
        for r in new_lines:
            a = awards.get(r["id"])
            conn.execute(db.rfx_line.insert().values(
                rfx_id=rfx_id,
                last_price=a["unit_price"] if a else r["last_price"],
                incumbent_vendor_id=a["vendor_id"] if a else r["incumbent_vendor_id"],
                **{k: r[k] for k in ("line_no", *LINE_FIELDS)},
            ))
        for q in new_qs:
            conn.execute(db.question.insert().values(rfx_id=rfx_id, **q))
        conn.execute(db.rfx.update().where(db.rfx.c.id == rfx_id).values(
            terms={**terms, **added_terms}))
        if target["title"] == DEFAULT_TITLE and not target["category"]:
            # "(copy)" keeps two events from sharing one name until the buyer renames it.
            conn.execute(db.rfx.update().where(db.rfx.c.id == rfx_id).values(
                title=f"{source_rfx['title']} (copy)", category=source_rfx["category"]))
        _audit(conn, actor, "rfx.import", rfx_id, {
            "source_rfx_id": source_rfx_id, "lines": len(new_lines),
            "questions": len(new_qs), "terms": sorted(added_terms)})
    vendor_ids = invited_ids(source_rfx_id)
    set_invited(rfx_id, sorted(set(invited_ids(rfx_id)) | set(vendor_ids)))
    return {
        "imported": len(new_lines),
        "skipped_existing": len(src_rows) - len(new_lines),
        "questions": len(new_qs),
        "terms": sorted(added_terms),
        "invited_vendors": len(vendor_ids),
        "source": source_rfx["title"],
        "source_number": copy.event_number(source_rfx),
    }


def reference_prices(rfx_id: int) -> dict[int, dict]:
    """Last price paid and supplier per line number. Buyer only: the RFx page shows these;
    snapshot(), the email, the Excel template and the vendor reply link never read them."""
    with db.connect() as conn:
        rows = conn.execute(
            sa.select(db.rfx_line.c.line_no, db.rfx_line.c.last_price,
                      db.vendor.c.name.label("supplier"))
            .outerjoin(db.vendor, db.vendor.c.id == db.rfx_line.c.incumbent_vendor_id)
            .where(db.rfx_line.c.rfx_id == rfx_id)
        ).mappings().all()
    return {r["line_no"]: {"price": r["last_price"], "supplier": r["supplier"]} for r in rows}


# --- Terms and questions -----------------------------------------------------------------


def set_terms(rfx_id: int, terms: dict[str, str | None], actor: str = "buyer") -> dict:
    """Merge terms; a None value removes that key."""
    with db.begin() as conn:
        current = dict(
            conn.execute(sa.select(db.rfx.c.terms).where(db.rfx.c.id == rfx_id)).scalar() or {}
        )
        for key, value in terms.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        conn.execute(db.rfx.update().where(db.rfx.c.id == rfx_id).values(terms=current))
        _audit(conn, actor, "rfx.terms", rfx_id, terms)
    return current


def upsert_questions(rfx_id: int, questions: list[dict], actor: str = "buyer") -> list[int]:
    """Add or change questions by q_no (None = add at the end)."""
    done = []
    with db.begin() as conn:
        existing = {
            r["q_no"]: r["id"]
            for r in conn.execute(
                sa.select(db.question.c.q_no, db.question.c.id).where(
                    db.question.c.rfx_id == rfx_id
                )
            ).mappings()
        }
        next_no = max([n for n in existing if n], default=0) + 1
        terms = conn.execute(sa.select(db.rfx.c.terms).where(db.rfx.c.id == rfx_id)).scalar()
        for q in questions:
            rule = q.get("pass_rule")
            if rule and rule.get("op") not in PASS_OPS:
                raise ValueError(f"pass rule op must be one of {PASS_OPS}")
            if "pass_rule" in q:
                q = {**q, "pass_rule": stored_rule(rule, terms or {})}
            q_no = q.get("q_no") or next_no
            next_no = max(next_no, q_no + 1)
            values = {
                k: q[k]
                for k in ("section", "text", "answer_type", "is_gating", "pass_rule")
                if k in q and (q[k] is not None or k == "pass_rule")
            }
            if q_no in existing:
                conn.execute(
                    db.question.update().where(db.question.c.id == existing[q_no]).values(**values)
                )
            else:
                if not values.get("text"):
                    raise ValueError(f"new question {q_no} needs text")
                conn.execute(
                    db.question.insert().values(
                        rfx_id=rfx_id, q_no=q_no, is_gating=values.pop("is_gating", False), **values
                    )
                )
            done.append(q_no)
        _audit(conn, actor, "rfx.questions.upsert", rfx_id, {"q_nos": done})
    return done


def remove_questions(rfx_id: int, q_nos: list[int], actor: str = "buyer") -> None:
    with db.begin() as conn:
        conn.execute(
            db.question.delete().where(
                db.question.c.rfx_id == rfx_id, db.question.c.q_no.in_(q_nos)
            )
        )
        _audit(conn, actor, "rfx.questions.remove", rfx_id, {"q_nos": q_nos})


# --- Completeness (code, not the model) --------------------------------------------------


def check_completeness(rfx_id: int) -> list[str]:
    """What would make replies hard to compare later. Empty list = complete."""
    snap = snapshot(rfx_id)
    issues = []
    if not snap["lines"]:
        issues.append("The RFx has no lines.")
    for line in snap["lines"]:
        name = f"Line {line['line_no']} ({line['description']})"
        if not line["canonical_uom"]:
            issues.append(f"{name}: no unit to compare prices in.")
        if not line["annual_qty"]:
            issues.append(f"{name}: no annual quantity.")
        if not line["spec"]:
            issues.append(
                f"{name}: no specification (size, grade or other attributes), so "
                "vendors may quote different items."
            )
    for q in snap["questions"]:
        if q["is_gating"] and not q["pass_rule"]:
            issues.append(f"Question {q['q_no']} is pass/fail but has no pass rule.")
    terms = standard_terms(snap["terms"])
    for key in TERM_KEYS_NEEDED:
        if not terms.get(key):
            issues.append(f"Terms: {copy.field_label(key).lower()} is not stated.")
    with db.connect() as conn:
        is_sandbox = conn.execute(sa.select(db.rfx.c.is_sandbox).where(
            db.rfx.c.id == rfx_id)).scalar()
    if is_sandbox:  # a sandbox is never sent: its vendors come with the replies added to it
        return issues
    if not terms.get("due"):
        issues.append("Terms: no reply-by date. Vendors need a date to quote by.")
    if not snap["invited_vendors"]:
        issues.append("No vendors invited yet (Vendors & send tab).")
    return issues


# --- Email, template and sending ---------------------------------------------------------


def _fmt_pairs(pairs: dict | None) -> str:
    """{'screen_size': '14-inch'} -> 'Screen size: 14-inch' (no internal field names)."""
    return ", ".join(f"{copy.field_label(k)}: {v}" for k, v in (pairs or {}).items())


LINK_PLACEHOLDER = "[each vendor's personal link is added when you send]"
TEMPLATE_NAME = "response_template.xlsx"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def render_email(rfx_id: int, reply_link: str | None = None) -> tuple[str, str]:
    """Subject and a short body built by code. The full request is in the attached RFx PDF
    (rfx_doc.rfx_pdf); the body only invites, gives the deadline and the key terms."""
    from level.agents.copilot import rfx_doc  # rfx_doc imports this module

    s = snapshot(rfx_id)
    due = s["terms"].get("due")
    deadline = (f"Please send your quotation by {rfx_doc.term_text(due)}.\n\n" if due
                else "")
    terms = "\n".join(f"  {t}" for t in rfx_doc.key_terms(s["terms"]))
    body = (
        "Dear Sir/Madam,\n\n"
        f"You are invited to quote for {s['title']}.\n\n"
        f"{deadline}"
        + (f"Key terms:\n{terms}\n\n" if terms else "")
        + "Full details are in the attached RFx: every line with its specification, quantity "
        "and delivery place, the terms and the questionnaire. An Excel response template is "
        "also attached; you may reply in any format.\n\n"
        f"You can also send your quote through your personal reply link: "
        f"{reply_link or LINK_PLACEHOLDER}\n\n"
        f"Regards,\n{copy.buyer_name()}"
    )
    return f"Request for quotation: {s['title']}", body


def attachments(rfx_id: int) -> list[tuple[str, str, bytes]]:
    """(file name, mime type, content) of what goes with the RFx email."""
    from level.agents.copilot import rfx_doc

    return [(rfx_doc.filename(rfx_id), "application/pdf", rfx_doc.rfx_pdf(rfx_id)),
            (TEMPLATE_NAME, XLSX_MIME, template_xlsx(rfx_id))]


def template_xlsx(rfx_id: int) -> bytes:
    """Optional response template: one row per line, a sheet for the questionnaire."""
    from openpyxl import Workbook

    s = snapshot(rfx_id)
    wb = Workbook()
    ws = wb.active
    ws.title = "Prices"
    ws.append(
        [
            "Line",
            "Description",
            "Specification",
            "Unit",
            "Annual qty",
            "Your price",
            "Currency",
            "Price per (unit)",
            "Notes",
        ]
    )
    for line in s["lines"]:
        ws.append(
            [
                line["line_no"],
                line["description"],
                _fmt_pairs(line["spec"]),
                line["canonical_uom"],
                line["annual_qty"],
                None,
                None,
                line["canonical_uom"],
                None,
            ]
        )
    qs = wb.create_sheet("Questionnaire")
    qs.append(["Q", "Section", "Question", "Your answer"])
    for q in s["questions"]:
        qs.append([q["q_no"], q["section"], q["text"], None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def set_invited(rfx_id: int, vendor_ids: list[int]) -> None:
    with db.begin() as conn:
        conn.execute(
            db.invitation.delete().where(
                db.invitation.c.rfx_id == rfx_id, db.invitation.c.vendor_id.not_in(vendor_ids)
            )
        )
        have = set(
            conn.execute(
                sa.select(db.invitation.c.vendor_id).where(db.invitation.c.rfx_id == rfx_id)
            ).scalars()
        )
        for vid in vendor_ids:
            if vid not in have:
                conn.execute(db.invitation.insert().values(rfx_id=rfx_id, vendor_id=vid))


def send(rfx_id: int) -> int:
    """Buyer's explicit click: one outbox email per invited vendor; the RFx becomes issued.

    Only the page calls this. The co-pilot has no tool that sends.
    """
    subject, _ = render_email(rfx_id)
    with db.begin() as conn:
        status = conn.execute(sa.select(db.rfx.c.status).where(db.rfx.c.id == rfx_id)).scalar()
        if status != "draft":
            return 0  # already sent: a double-click sends nothing twice
        me = conn.execute(sa.select(db.rfx.c.title, db.rfx.c.terms).where(
            db.rfx.c.id == rfx_id)).mappings().one()
        twin = conn.execute(sa.select(db.rfx.c.id).where(
            db.rfx.c.id != rfx_id, db.rfx.c.title == me["title"],
            db.rfx.c.status.in_(["issued", "collecting", "ready"]))).first()
        if twin:  # two live events with one name cannot be told apart (UX audit B10)
            raise ValueError("Another event that is still open has this title. Change the "
                             "title before sending.")
        if not (me["terms"] or {}).get("due"):
            raise ValueError("Set a reply-by date on the Terms tab before sending.")
        vendors = (
            conn.execute(
                sa.select(db.vendor)
                .join(db.invitation, db.invitation.c.vendor_id == db.vendor.c.id)
                .where(db.invitation.c.rfx_id == rfx_id)
            )
            .mappings()
            .all()
        )
        if not vendors:
            raise ValueError("Invite at least one vendor before sending.")
        files = []  # the same RFx PDF and template go to every vendor
        for name, mime, content in attachments(rfx_id):
            sha = hashlib.sha256(content).hexdigest()
            path = db.upload_dir() / f"{sha}{name[name.rfind('.'):]}"
            path.write_bytes(content)
            files.append((name, mime, path, sha, len(content)))
        for v in vendors:
            _, body = render_email(rfx_id, link_url(rfx_id, v["id"]))
            msg_id = conn.execute(
                db.message.insert().values(
                    rfx_id=rfx_id,
                    vendor_id=v["id"],
                    direction="out",
                    channel="email",
                    status="sent",
                    subject=subject,
                    recipient=v["email"],
                    body=body,
                    received_at=db.now_iso(),
                )
            ).inserted_primary_key[0]
            for name, mime, path, sha, size in files:
                conn.execute(
                    db.attachment.insert().values(
                        message_id=msg_id, filename=name, kind="other", mime=mime,
                        path=str(path), sha256=sha, size_bytes=size,
                    )
                )
        conn.execute(
            db.rfx.update()
            .where(db.rfx.c.id == rfx_id)
            .values(status="issued", issued_at=db.now_iso())
        )
        _audit(conn, "buyer", "rfx.sent", rfx_id, {"vendors": len(vendors)})
    return len(vendors)


def as_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# --- Helpers for the page's table editors ------------------------------------------------


def describe_spec(pairs: dict | None) -> str:
    """Read-only form of a spec for screens: 'Ply: 3, Flute: B'."""
    return _fmt_pairs(pairs)


def format_pairs(pairs: dict | None) -> str:
    """{'size': 'M', 'grade': 'A'} -> 'size=M; grade=A' (editable as one text cell)."""
    return "; ".join(f"{k}={v}" for k, v in (pairs or {}).items())


def parse_pairs(text: str | None) -> dict:
    """'size=M; grade=A' -> {'size': 'M', 'grade': 'A'}. Parts without '=' are rejected."""
    out = {}
    for part in (text or "").split(";"):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"write attributes as name=value; got {part.strip()!r}")
        key, value = part.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def format_rule(rule: dict | None) -> str:
    """A pass rule in words: '10 or less', 'Must be yes', 'Valid on 14 Mar 2025'."""
    if not rule:
        return ""
    op, value = rule.get("op", ""), rule.get("value")
    if op == "is_yes" or (op == "==" and str(value).lower() in ("yes", "true")):
        return "Must be yes"
    if op == "==" and str(value).lower() in ("no", "false"):
        return "Must be no"
    if op == "valid_on_date":
        return "Valid on the award date"
    if op == "valid_on":
        return f"Valid on {copy.date(str(value))}" if value else "Valid on the award date"
    words = {"<=": "{} or less", "<": "Less than {}", ">=": "{} or more", ">": "More than {}",
             "==": "Equal to {}", "contains": "Mentions {}"}
    return words.get(op, op + " {}").format(value or "").strip()


# Pass rules typed in words, as format_rule writes them. (pattern, op); the number or date
# is the pattern's group 1.
_RULE_WORDS = [
    (r"(?:must be )?yes", "=="), (r"(?:must be )?no", "=="),
    (r"([\d.]+) or less", "<="), (r"(?:at most|no more than|up to) ([\d.]+)", "<="),
    (r"([\d.]+) or more", ">="), (r"(?:at least|no less than) ([\d.]+)", ">="),
    (r"less than ([\d.]+)", "<"), (r"more than ([\d.]+)", ">"),
    (r"equal to ([\d.]+)", "=="), (r"valid on (?:the )?award date", "valid_on_date"),
    (r"valid on (.+)", "valid_on"),
]
RULE_HELP = ("Write the pass rule in words: '10 or less', '5 or more', 'Must be yes' or "
             "'Valid on the award date'.")


def parse_rule(text: str | None) -> dict | None:
    """'10 or less' -> {'op': '<=', 'value': '10'}; 'Must be yes' -> {'op': '==', 'value':
    'yes'}. The short forms '<= 10' and 'is_yes' are accepted too."""
    from level.core.gating import parse_date

    text = (text or "").strip()
    if not text:
        return None
    op, _, value = text.partition(" ")
    if op in PASS_OPS:
        return {"op": op, "value": value.strip() or None}
    for pattern, rule_op in _RULE_WORDS:
        m = re.fullmatch(pattern, text, re.I)
        if not m:
            continue
        if rule_op == "==" and not m.groups():
            return {"op": "==", "value": "yes" if "yes" in text.lower() else "no"}
        if rule_op == "valid_on_date":
            return {"op": "valid_on_date", "value": None}
        if rule_op == "valid_on":
            day = parse_date(m.group(1))
            if day is None:
                raise ValueError(f"Could not read the date in '{text}'. {RULE_HELP}")
            return {"op": "valid_on", "value": day.isoformat()}
        return {"op": rule_op, "value": m.group(1)}
    raise ValueError(f"'{text}' is not a pass rule we can check. {RULE_HELP}")


def stored_rule(rule: dict | None, terms: dict) -> dict | None:
    """The rule as the requirement check reads it: 'is_yes' -> '== yes'; 'valid on the award
    date' -> 'valid_on <award date>' once the draft has an award date."""
    if not rule:
        return rule
    if rule.get("op") == "is_yes":
        return {**rule, "op": "==", "value": "yes"}
    if rule.get("op") == "valid_on_date" and terms.get("award_date"):
        return {**rule, "op": "valid_on", "value": terms["award_date"]}
    return rule


def apply_line_edits(
    rfx_id: int, rows: list[dict], edited: dict, added: list, deleted: list
) -> None:
    """Apply only what the buyer changed in the lines table (Streamlit's data_editor delta).

    rows are the table as shown; edited maps row position -> changed columns. Text columns
    'spec' and 'conversion_factors' hold name=value pairs.
    """

    def convert(change: dict) -> dict:
        out = dict(change)
        for key in ("spec", "conversion_factors"):
            if key in out:
                out[key] = parse_pairs(out[key])
        return out

    if deleted:
        remove_lines(rfx_id, [rows[i]["line_no"] for i in deleted])
    changes = [{"line_no": rows[int(i)]["line_no"], **convert(c)} for i, c in edited.items()]
    changes += [convert({k: v for k, v in a.items() if v not in (None, "")}) for a in added]
    changes = [c for c in changes if len(c) > 1 or "line_no" not in c]
    if changes:
        upsert_lines(rfx_id, changes)


def apply_question_edits(
    rfx_id: int, rows: list[dict], edited: dict, added: list, deleted: list
) -> None:
    def convert(change: dict) -> dict:
        out = dict(change)
        if "pass_rule" in out:
            out["pass_rule"] = parse_rule(out["pass_rule"])
        return out

    if deleted:
        remove_questions(rfx_id, [rows[i]["q_no"] for i in deleted])
    changes = [{"q_no": rows[int(i)]["q_no"], **convert(c)} for i, c in edited.items()]
    changes += [convert({k: v for k, v in a.items() if v not in (None, "")}) for a in added]
    if changes:
        upsert_questions(rfx_id, changes)


def version(rfx_id: int) -> int:
    """Changes whenever the draft changes (newest audit id), so editors reload fresh data."""
    with db.connect() as conn:
        return (
            conn.execute(
                sa.select(sa.func.max(db.audit_event.c.id)).where(
                    db.audit_event.c.target_kind == "rfx", db.audit_event.c.target_id == rfx_id
                )
            ).scalar()
            or 0
        )


def all_vendors() -> list[dict]:
    with db.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(sa.select(db.vendor).order_by(db.vendor.c.name)).mappings()
        ]


def invited_ids(rfx_id: int) -> list[int]:
    with db.connect() as conn:
        return list(
            conn.execute(
                sa.select(db.invitation.c.vendor_id).where(db.invitation.c.rfx_id == rfx_id)
            ).scalars()
        )


def add_vendor(name: str, email: str | None) -> int:
    if not name.strip():
        raise ValueError("vendor name is required")
    with db.begin() as conn:
        return conn.execute(
            db.vendor.insert().values(name=name.strip(), email=email or None)
        ).inserted_primary_key[0]
