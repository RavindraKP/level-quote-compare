"""The receipt behind one price in the grid. Layout only; data comes from data.receipt()."""

from __future__ import annotations

import streamlit as st

from components import data, review_data, source_viewer
from level import copy
from level.agents.analyst import audit
from level.types import CellState, Receipt


def show(r: Receipt, line: dict, vendor: dict, key: str) -> None:
    c = r.cell
    uom = line["canonical_uom"]
    st.markdown(f"**Line {line['line_no']}. {line['description']}** · {vendor['name']}")
    settled = (f" · verified by {copy.CLOSED_BY[c.confirmed_by.value]}"
               if c.confirmed_by and c.state == CellState.CONFIRMED else "")
    st.markdown(f"{data.STATE_SYMBOL[c.state]} **{data.STATE_LABEL[c.state]}**: "
                f"{copy.indian_numbers(copy.plain(c.reason))}{settled}")
    if c.state == CellState.AWAITING:
        st.info("This vendor has not replied yet. Their price appears here once the reply is "
                "read.")
        return

    q = r.claim
    if q is None:
        st.info("Not quoted: no price for this line was found in the vendor's reply. It is "
                "never priced as zero.")
    else:
        st.markdown(f"**As quoted:** {c.as_quoted or q.raw_price or '—'}  \n"
                    f"Item as the vendor wrote it: *{q.vendor_item_text}*")
        if q.evidence.quote:  # the vendor's words once on the receipt (UX audit M11)
            source_viewer.quote(q.evidence.quote)

    if r.conversion_steps:
        st.markdown(f"**Converted to rupees per {uom}**")
        rows = []
        for i, s in enumerate(r.conversion_steps, start=1):
            text, source = copy.conversion_step(s.label, s.operation, s.input, s.output,
                                                s.source)
            rows.append(f"{i}. {text}" + (f"  \n   <small>{source}</small>" if source else ""))
        st.markdown("\n".join(rows), unsafe_allow_html=True)
    prices = []
    if c.unit_price_inr is not None:
        prices.append(f"per {uom}, ex-GST, ex-works: **{copy.unit_price(c.unit_price_inr)}**")
    if c.state != CellState.NOT_QUOTED:
        landed = copy.unit_price(c.landed_price_inr) if c.landed_price_inr is not None \
            else "unknown"
        prices.append(f"delivered: **{landed}**")
    if prices:
        st.markdown("  \n".join(prices))

    if r.assumptions_used:
        st.markdown("**Assumptions used**")
        st.markdown("\n".join(f"- {copy.assumption_label(k)}: "
                              f"{copy.indian_numbers(copy.plain(copy.assumption_value(k, v)))}"
                              for k, v in r.assumptions_used.items()))

    if r.checks:
        st.markdown("**Checks**")

        def mark(ch) -> str:
            if ch.detail.startswith("Not checked"):
                return "– not run"  # e.g. a photo has no text layer to match against
            return "✓ passed" if ch.passed else "✖ did not pass"

        st.markdown("\n".join(f"- {mark(ch)} · {ch.name}: "
                              f"{copy.indian_numbers(copy.plain(ch.detail, capitalise=False))}"
                              for ch in r.checks))

    # Only this line's points, each once (a point raised on a price that covers five lines
    # is listed once, not five times: UX audit M11).
    points, seen = [], set()
    for f in r.flags:
        if f.rfx_line_id not in (None, line["id"]) or f.id in seen:
            continue
        seen.add(f.id)
        status = ("open" if f.state == "open" else
                  f"settled by {copy.CLOSED_BY[f.closed_by.value] if f.closed_by else 'someone'}")
        points.append(f"- {copy.issue_title(f.type, f.reason, f.target_kind)} ({status}): "
                      f"{copy.indian_numbers(copy.plain(f.reason, capitalise=False))}")
    if points:
        st.markdown("**Points raised**")
        st.markdown("\n".join(points))

    with st.expander("Source", expanded=q is not None):
        source_viewer.show(r.evidence, key=f"{key}_src", show_quote=False,
                           email_body=review_data.run_email_body(q.run_id) if q else None)

    # This price's own history, in words: decisions on its points, the vendor's answers and
    # confirmations, assumption changes. Never the stored records (UX audit B6, M11).
    history = audit.for_line(c.rfx_id, vendor["id"], line["id"])
    if history:
        with st.expander("Change history"):
            st.dataframe([{"When": h["when"], "Who": h["who"], "What": h["what"],
                           "Details": h["details"]} for h in history],
                         hide_index=True, width="stretch")
