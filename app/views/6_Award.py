"""Award (WP6, polished in WP12): two states. Before the award, an approval-ready summary
of the saved award options and the decision. After it, the result first (tiles, memo,
workbooks, emails waiting), with caveats, the other options and the audit trail folded away.
Layout and calls only.
"""

from html import escape

import pandas as pd
import streamlit as st
from components import data, frame, style

from level import copy
from level.agents.analyst import award
from level.core import summary

rfx = data.current_rfx(st.session_state.get("rfx_id"))
frame.header(rfx, "Award")
if rfx is None:
    st.info("No event yet. Create one on the RFx page; once replies are compared, award "
            "options appear here.")
    st.stop()

current = award.awarded(rfx["id"])

# --- Save a recent award option (before the award only) -----------------------------------
recent = [s for s in award.scenarios(rfx["id"]) if not s["pinned"]]
if recent and not current:
    with st.expander("Add a recent award option from the Analyst"):
        choice = st.selectbox("Award option", recent,
                              format_func=lambda s: copy.plain(s["name"]))
        if st.button("Add to this page"):
            award.set_pinned(choice["id"], True)
            st.rerun()

pinned = award.scenarios(rfx["id"], pinned_only=True)
if current and current["id"] not in {s["id"] for s in pinned}:
    pinned.insert(0, current)  # the awarded option always stays on this page
if not pinned:
    st.info("No award options saved yet. Ask the Analyst for one (for example a split "
            "between two vendors, or everything to one vendor) and press Save under the "
            "answer.")
    st.stop()


def label(s: dict) -> str:
    return copy.plain(s["name"])


def show_caveats(caveats: list[str], folded: bool = True) -> None:
    """At most five caveats that change the number, the price basis in one line, and the
    vendors' terms folded away (UX audit M2). Inside an expander (folded=False) the extra
    caveats are listed in place, since Streamlit does not allow an expander in an expander."""
    parts = copy.caveat_parts(caveats)
    if parts["main"] or parts["basis"]:
        style.note("<b>Risks and caveats</b><br>" + "<br>".join(
            f"· {escape(c)}" for c in parts["main"])
            + (f"<br><span class='lv-dim'>{escape(' '.join(parts['basis']))}</span>"
               if parts["basis"] else ""))
    for title, items in ((f"More caveats ({len(parts['more'])})", parts["more"]),
                         (copy.terms_noted(len(parts["terms"])), parts["terms"])):
        if not items:
            continue
        if folded:
            with st.expander(title):
                st.markdown("\n".join(f"- {c}" for c in items))
        else:
            st.markdown(f"**{title}**\n\n" + "\n".join(f"- {c}" for c in items))
    if not any(parts.values()):
        st.caption("No caveats recorded for this option.")


def show_tiles(h: dict) -> None:
    style.tiles([
        ("Annual value", copy.inr_short(h["total_inr"]), copy.inr(h["total_inr"])),
        ("Saving against last year", copy.inr_short(h["savings_inr"]),
         copy.pct(h["savings_pct"]) + " on the same lines"),
        ("Vendors · lines", f"{h['vendors_used']} · {h['lines_allocated']}",
         f"{copy.plural(h['lines_without_quote'], 'line')} without a price"),
        ("Prices not yet verified", str(h["unconfirmed_count"]),
         f"{copy.inr_short(h['exposure_inr'])} at stake if 10% off"),
    ])


def vendor_split(option: dict) -> None:
    """Who wins what in this option; the figures come from level/core/summary.py."""
    names = {v["id"]: v["name"] for v in data.all_vendors()}
    rows = summary.vendor_split(option["result"])
    if not rows:
        st.caption("No line in this option has a price, so there is no split by vendor.")
        return
    st.markdown("**Vendor split**")
    st.dataframe(pd.DataFrame([{
        "Vendor": names.get(r["vendor_id"], "Unknown vendor"),
        "Lines won": r["lines"],
        "Annual value": copy.inr(r["total_inr"]),
        "Share of total": copy.pct(r["share_pct"]),
        "Prices not yet verified": r["unconfirmed_lines"]} for r in rows]),
        hide_index=True, width="stretch",
        column_config={c: st.column_config.Column(alignment="right") for c in
                       ("Lines won", "Annual value", "Share of total",
                        "Prices not yet verified")})
    lines = {ln["id"]: ln for ln in data.lines(rfx["id"])}
    with st.expander("Line-by-line allocation"):
        st.dataframe(pd.DataFrame([{
            "Line": lines[a.rfx_line_id]["line_no"] if a.rfx_line_id in lines else "—",
            "Description": lines.get(a.rfx_line_id, {}).get("description", ""),
            "Vendor": names.get(a.vendor_id, "Unknown vendor") if a.vendor_id
            else "No price",
            "Unit price": copy.unit_price(a.unit_price_inr),
            "Annual quantity": f"{a.annual_qty:,.0f}",
            "Annual value": copy.inr(a.line_total_inr),
            "Status": copy.STATE_LABEL.get(a.cell_state, "—") if a.cell_state else "—"}
            for a in option["result"].allocation]), hide_index=True, width="stretch")


ROWS = [("Annual value", lambda h: copy.inr(h["total_inr"])),
        ("Last year, same lines", lambda h: copy.inr(h["last_year_total_inr"])),
        ("Saving against last year", lambda h: copy.inr(h["savings_inr"])),
        ("Saving %", lambda h: copy.pct(h["savings_pct"])),
        ("Vendors used", lambda h: str(h["vendors_used"])),
        ("Lines allocated", lambda h: str(h["lines_allocated"])),
        ("Lines without a price", lambda h: str(h["lines_without_quote"])),
        ("Prices not yet verified", lambda h: str(h["unconfirmed_count"])),
        ("Rupees at stake if those are 10% off", lambda h: copy.inr(h["exposure_inr"])),
        ("Lines where a 10% move changes the winner", lambda h: str(h["lines_winner_changes"]))]


def side_by_side(options: list[dict], removable: bool) -> None:
    """The saved options as columns; before the award each can be taken off this page."""
    table = pd.DataFrame({label(s) + (" · awarded" if s["is_award"] else ""):
                          [fmt(s["headline"]) for _, fmt in ROWS] for s in options},
                         index=[name for name, _ in ROWS])
    st.dataframe(table, width="stretch",  # fixed widths keep long option names from hiding values
                 column_config={"_index": st.column_config.TextColumn(width=280)}
                 | {c: st.column_config.TextColumn(alignment="right", width=300,
                                                    help=c) for c in table.columns})
    if not removable:
        return
    cols = st.columns(len(options))
    for col, s in zip(cols, options, strict=True):
        if col.button("Remove from this page", key=f"unpin_{s['id']}",
                      disabled=bool(s["is_award"]),
                      help="The awarded option stays on this page." if s["is_award"] else
                      f"Removes '{copy.plain(s['name'])}' from the comparison; it stays "
                      "in the Analyst's history."):
            award.set_pinned(s["id"], False)
            st.rerun()


if note := st.session_state.pop("award_toast", None):
    st.toast(note)  # set before the rerun that shows the result
if current:
    # --- After the award: the result first, the working folded away -------------------------
    when = next((r["When"] for r in data.audit_trail(rfx["id"]) if r["What"] == "Awarded"),
                None)
    st.success(f"Awarded: {label(current)}" + (f" · {when}" if when else ""))
    show_tiles(current["headline"])
    st.caption("Prices not yet verified are listed on page one of the award memo.")
    vendor_split(current)
    c1, c2, c3 = st.columns(3)
    # Files are built when the button is pressed, not on every page load (UX audit B1).
    c1.download_button("Award memo (Word)",
                       lambda: award.memo_docx(rfx["id"], current["id"]),
                       file_name="award-memo.docx", on_click="ignore", type="primary")
    c2.download_button("Award option workbook (Excel)",
                       lambda: award.scenario_xlsx(rfx["id"], current["id"]),
                       file_name="award-option.xlsx", on_click="ignore")
    c3.download_button("Comparison workbook (Excel)",
                       lambda: award.comparison_xlsx(rfx["id"]),
                       file_name="comparison.xlsx", on_click="ignore")
    drafts = award.drafts(rfx["id"])
    st.markdown(f"**Emails waiting to be sent ({len(drafts)})**. Nothing is sent "
                "automatically; open one to edit and send it.")
    for d in drafts:
        with st.expander(f"{d['vendor']}: {d['subject']}"):
            subject = st.text_input("Subject", d["subject"], key=f"as_{d['id']}")
            body = st.text_area("Email", d["body"], height=300, key=f"ab_{d['id']}")
            if st.button("Send email", key=f"asend_{d['id']}"):
                data.send_draft(d["id"], subject, body)
                st.toast(f"Sent to {d['vendor']} (simulated).")
                st.rerun()

    with st.expander("Caveats for this award"):
        show_caveats(current["result"].caveats, folded=False)
    if len(pinned) > 1:
        with st.expander("Compare with the other saved options"):
            side_by_side(pinned, removable=False)
else:
    # --- Before the award: approval-ready summary of the option being looked at -----------
    # Nothing is pre-selected as a "recommendation" (UX audit H6): the buyer picks an option.
    CHOICE_KEY = f"award_choice_{rfx['id']}"
    by_id = {s["id"]: s for s in pinned}
    ids = list(by_id)
    open_points = data.open_issue_count(rfx["id"])
    if open_points:
        st.warning(f"{copy.plural(open_points, 'point')} still open on Review. You can award "
                   "now; prices not yet verified are listed on page one of the award memo.")
        st.page_link("views/3_Review.py", label="Open Review")

    st.subheader("Decision")
    chosen_id = st.radio("Pick the award option to award", ids, index=None, key=CHOICE_KEY,
                         horizontal=len(pinned) <= 3,
                         format_func=lambda sid: label(by_id[sid]))
    cheapest = min((s for s in pinned if s["headline"]["total_inr"] is not None),
                   key=lambda s: s["headline"]["total_inr"], default=None)
    if cheapest and len(pinned) > 1:
        st.caption(f"Lowest annual value of the saved options: {label(cheapest)} "
                   f"({copy.inr_short(cheapest['headline']['total_inr'])}).")
    lead = by_id.get(chosen_id)
    if lead is None:
        st.info("Pick an option above to see its summary and award it.")
    else:
        st.markdown(f"**Selected option: {escape(label(lead))}**")
        show_tiles(lead["headline"])
        show_caveats(lead["result"].caveats)
        vendor_split(lead)
        st.caption("Prices not yet verified do not stop an award; they are listed on page one "
                   "of the award memo.")
        h = lead["headline"]
        b1, b2, _ = st.columns([1, 1, 2])
        with b1.popover("Award to these vendors", type="primary"):
            st.markdown(
                f"**Award {escape(label(lead))}?**\n\n"
                f"- Annual value: {copy.inr(h['total_inr'])}\n"
                f"- {copy.plural(h['vendors_used'], 'vendor')}, "
                f"{copy.plural(h['lines_allocated'], 'line')}\n"
                f"- {copy.plural(h['unconfirmed_count'], 'price')} not yet verified: "
                f"{copy.inr(h['exposure_inr'])} at stake if 10% off\n\n"
                "Questions are then drafted to the winning vendors with open points; "
                "nothing is sent until you press Send.")
            if st.button("Confirm award", type="primary", key="confirm_award"):
                with st.spinner("Recording the award and drafting questions to the winning "
                                "vendors…"):
                    drafted = award.award(rfx["id"], lead["id"])
                st.session_state["award_toast"] = (
                    f"Awarded. {copy.plural(len(drafted), 'email draft')} waiting for you "
                    "to send.")
                st.rerun()
        # Built when pressed, not on every page load (UX audit B1).
        b2.download_button("Preview award memo (Word)",
                           lambda: award.memo_docx(rfx["id"], lead["id"]),
                           file_name="award-memo-preview.docx", on_click="ignore",
                           key="memo_preview")

    if len(pinned) > 1:
        st.subheader("Options side by side")
    side_by_side(pinned, removable=True)

# --- Audit trail ----------------------------------------------------------------------------
with st.expander("Audit trail: who changed what, and when"):
    trail = data.audit_trail(rfx["id"])
    who = st.segmented_control("Who", ["Everyone", "You", "Vendors", "Checks"],
                               default="Everyone", key="audit_who")
    keep = {"You": "buyer", "Vendors": "vendor", "Checks": "system"}.get(who or "Everyone")
    rows = [r for r in trail if keep is None or r["actor"] == keep]
    if not rows:
        st.caption("Nothing recorded yet for this filter.")
    else:
        st.dataframe([{k: v for k, v in r.items() if k != "actor"} for r in rows],
                     hide_index=True, width="stretch")
