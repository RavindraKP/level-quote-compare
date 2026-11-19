"""Award (WP6, polished in WP12): an approval-ready summary of the saved award options,
then the decision, the exports, the emails drafted and the audit trail. Layout and calls only.
"""

from html import escape

import pandas as pd
import streamlit as st
from components import data, frame, style

from level import copy
from level.agents.analyst import award

rfx = data.current_rfx(st.session_state.get("rfx_id"))
frame.header(rfx, "Award")
if rfx is None:
    st.info("No event yet. Create one on the RFx page; once replies are compared, award "
            "options appear here.")
    st.stop()

# --- Save a recent award option -----------------------------------------------------------
recent = [s for s in award.scenarios(rfx["id"]) if not s["pinned"]]
if recent:
    with st.expander("Add a recent award option from the Analyst"):
        choice = st.selectbox("Award option", recent,
                              format_func=lambda s: copy.plain(s["name"]))
        if st.button("Add to this page"):
            award.set_pinned(choice["id"], True)
            st.rerun()

pinned = award.scenarios(rfx["id"], pinned_only=True)
if not pinned:
    st.info("No award options saved yet. Ask the Analyst for one (for example a split "
            "between two vendors, or everything to one vendor) and press Save under the "
            "answer.")
    st.stop()


def label(s: dict) -> str:
    return copy.plain(s["name"])


def show_caveats(caveats: list[str]) -> None:
    """At most five caveats that change the number, the price basis in one line, and the
    vendors' terms folded away (UX audit M2)."""
    parts = copy.caveat_parts(caveats)
    if parts["main"] or parts["basis"]:
        style.note("<b>Risks and caveats</b><br>" + "<br>".join(
            f"· {escape(c)}" for c in parts["main"])
            + (f"<br><span class='lv-dim'>{escape(' '.join(parts['basis']))}</span>"
               if parts["basis"] else ""))
    if parts["more"]:
        with st.expander(f"More caveats ({len(parts['more'])})"):
            st.markdown("\n".join(f"- {c}" for c in parts["more"]))
    if parts["terms"]:
        with st.expander(copy.terms_noted(len(parts["terms"]))):
            st.markdown("\n".join(f"- {c}" for c in parts["terms"]))


# --- Approval-ready summary: the option being looked at, savings, risks on one screen ------
# Nothing is pre-selected as a "recommendation" (UX audit H6): the buyer picks an option.
current = award.awarded(rfx["id"])
CHOICE_KEY = f"award_choice_{rfx['id']}"
by_id = {s["id"]: s for s in pinned}
ids = list(by_id)
open_points = data.open_issue_count(rfx["id"])
if open_points and not current:
    st.warning(f"{copy.plural(open_points, 'point')} still open on Review. You can award now; "
               "prices not yet verified are listed on page one of the award memo.")
    st.page_link("views/3_Review.py", label="Open Review")

st.subheader("Decision" if not current else "Awarded")
chosen_id = st.radio(
    "Pick an award option to look at" if current else "Pick the award option to award", ids,
    index=ids.index(current["id"]) if current and current["id"] in ids else None,
    key=CHOICE_KEY, horizontal=len(pinned) <= 3,
    format_func=lambda sid: label(by_id[sid]) + (" · awarded" if by_id[sid]["is_award"]
                                                 else ""))
cheapest = min((s for s in pinned if s["headline"]["total_inr"] is not None),
               key=lambda s: s["headline"]["total_inr"], default=None)
if cheapest and len(pinned) > 1:
    st.caption(f"Lowest annual value of the saved options: {label(cheapest)} "
               f"({copy.inr_short(cheapest['headline']['total_inr'])}).")
lead = by_id.get(chosen_id)
if lead is None:
    st.info("Pick an option above to see its summary and award it.")
else:
    h = lead["headline"]
    st.markdown(f"**Selected option: {escape(label(lead))}**")
    style.tiles([
        ("Annual value", copy.inr_short(h["total_inr"]), copy.inr(h["total_inr"])),
        ("Saving against last year", copy.inr_short(h["savings_inr"]),
         copy.pct(h["savings_pct"]) + " on the same lines"),
        ("Vendors · lines", f"{h['vendors_used']} · {h['lines_allocated']}",
         f"{copy.plural(h['lines_without_quote'], 'line')} without a price"),
        ("Prices not yet verified", str(h["unconfirmed_count"]),
         f"{copy.inr_short(h['exposure_inr'])} at stake if 10% off"),
    ])
    show_caveats(lead["result"].caveats)
    st.caption("Prices not yet verified do not stop an award; they are listed on page one of "
               "the award memo.")
    if not lead["is_award"] and st.button("Award to these vendors", type="primary"):
        with st.spinner("Recording the award and drafting questions to vendors with open "
                        "points…"):
            drafted = award.award(rfx["id"], lead["id"])
        st.toast(f"Awarded. {copy.plural(len(drafted), 'email draft')} waiting below for you "
                 "to send.")
        st.rerun()

# --- Options side by side -----------------------------------------------------------------
if len(pinned) > 1:
    st.subheader("Options side by side")
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
table = pd.DataFrame({label(s): [fmt(s["headline"]) for _, fmt in ROWS] for s in pinned},
                     index=[name for name, _ in ROWS])
st.dataframe(table, width="stretch",  # fixed widths keep long option names from hiding values
             column_config={"_index": st.column_config.TextColumn(width=280)}
             | {c: st.column_config.TextColumn(alignment="right", width=300,
                                                help=c) for c in table.columns})
cols = st.columns(len(pinned))
for col, s in zip(cols, pinned, strict=True):
    if col.button("Remove from this page", key=f"unpin_{s['id']}",
                  help=f"Removes '{copy.plain(s['name'])}' from the comparison; it stays "
                       "in the Analyst's history."):
        award.set_pinned(s["id"], False)
        st.rerun()

# --- After the award: exports and the drafted emails ---------------------------------------
current = award.awarded(rfx["id"])
if current:
    st.success(f"Awarded: {label(current)}")
    c1, c2, c3 = st.columns(3)
    # Files are built when the button is pressed, not on every page load (UX audit B1).
    c1.download_button("Award memo (Word)",
                       lambda: award.memo_docx(rfx["id"], current["id"]),
                       file_name="award-memo.docx", on_click="ignore")
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
