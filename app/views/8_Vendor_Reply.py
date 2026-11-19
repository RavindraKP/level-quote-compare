"""Vendor reply (WP11): what a vendor sees through their personal link. Layout and calls only.

Opened only by Home.py after the link's token is verified; the scope (one RFx, one vendor)
comes from that token. This page imports nothing that reads buyer-side data.
"""

from decimal import Decimal

import streamlit as st

from level import copy
from level.agents import vendor_copilot
from level.agents.vendor_copilot import agent, answers, questions

AVATAR = {"user": ":material/person:", "assistant": ":material/support_agent:"}
# How each line's status reads to the vendor (the codes come from questions.quote_summary).
STATUS_WORDS = {"read": "Please check", "confirmed by you": "Confirmed by you",
                "question open": "Question open", "not offered": "Not offered",
                "no price": "No price", "could not read": "Could not read"}

scope = st.session_state["vendor_scope"]
rfx = questions.rfx_summary(scope)

st.title(f"Your quote: {rfx['title']}")
st.caption(f"For {rfx['vendor']}. Only you and the buyer see what you send here.")
doc_name, doc_pdf = questions.rfx_document(scope)
st.download_button("Download the RFx", doc_pdf, file_name=doc_name, mime="application/pdf",
                   help="The full request: lines, specifications, terms and questionnaire.")

with st.expander("The request (lines, units, terms)"):
    st.dataframe(
        [{"Line": ln["line_no"], "Item": ln["description"],
          "Specification": copy.spec_text(ln["spec"]),
          "Priced per": ln["canonical_uom"],
          "Annual qty": copy.number(Decimal(str(ln["annual_qty"])))
          if ln["annual_qty"] not in (None, "") else "—",
          "Deliver to": ln["plant"] or "—"} for ln in rfx["lines"]],
        hide_index=True, width="stretch",
        column_config={"Line": st.column_config.NumberColumn(width=50),
                       "Specification": st.column_config.TextColumn(width="large"),
                       "Annual qty": st.column_config.TextColumn(alignment="right")})
    if terms := copy.term_rows(rfx["terms"]):
        st.markdown("**Terms**")
        st.dataframe(terms, hide_index=True, width="stretch",
                     column_config={"Term": st.column_config.TextColumn(width="medium")})

# --- 1. Drop in a quote -------------------------------------------------------------------
has_reply = questions.latest_reply(scope) is not None
st.subheader("1. Send your quote, in any format")
with st.expander("Send a new or updated quote" if has_reply else "Send your quote",
                 expanded=not has_reply):
    if has_reply:
        st.caption("We have your quote. Send another file only if your prices have changed.")
    with st.form("drop", clear_on_submit=True, border=False):
        files = st.file_uploader("Excel, PDF, Word, a photo...", accept_multiple_files=True)
        body = st.text_area("Or type your prices here", height=100)
        if st.form_submit_button("Send my quote", type="primary"):
            try:
                vendor_copilot.submit_reply(scope, [(f.name, f.getvalue()) for f in files or []],
                                            body)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


@st.fragment(run_every=2)
def progress() -> None:
    """Re-renders on its own while the quote is read; reruns the page once it is done."""
    job = vendor_copilot.reply_job(scope)
    if job is None or job.status == "done":
        if st.session_state.get("was_reading"):
            st.session_state["was_reading"] = False
            st.rerun()
        return
    if job.status == "failed":
        st.error("We could not read that file. Please send it as Excel or PDF, or type your "
                 "prices in the box above.")
        return
    st.session_state["was_reading"] = True
    st.progress(job.progress, text="Reading your quote: " + copy.reading_stage(
        job.stage, "", len(rfx["lines"])).split(": ", 1)[1])


progress()
job = vendor_copilot.reply_job(scope)
if job is None or job.status in ("queued", "running"):
    st.stop()  # nothing to ask until the quote has been read

# --- 2. Questions ---------------------------------------------------------------------------
st.subheader("2. A few questions about your quote")
open_qs = questions.open_questions(scope)
if not agent.history(scope) and open_qs:
    with st.spinner("Preparing a few questions about your quote…"):
        agent.run_turn(scope, "")  # the co-pilot opens with the first question
for m in agent.history(scope):
    role = "user" if m["role"] == "user" else "assistant"
    with st.chat_message(role, avatar=AVATAR[role]):
        st.markdown(m["text"])
if open_qs:
    with st.expander(f"Questions still open ({len(open_qs)})"):
        for n, q in enumerate(open_qs, start=1):
            lines = (f" (line{'s' if len(q.line_nos) > 1 else ''} "
                     f"{', '.join(map(str, q.line_nos))})" if q.line_nos else "")
            st.markdown(f"**Question {n}**{lines}: {q.text}")
else:
    st.success("No open questions. Please check the summary below and confirm.")
# Disabled while an answer is being saved, so a second message cannot cut the first short.
said = st.chat_input("Your answer", submit_mode="disable")
if said:
    with st.chat_message("user", avatar=AVATAR["user"]):
        st.markdown(said)
    with st.chat_message("assistant", avatar=AVATAR["assistant"]), \
            st.spinner("Saving your answer…"):
        agent.run_turn(scope, said)  # saved to the thread; shown after the rerun
    st.rerun()


# --- 3. Summary and confirm -----------------------------------------------------------------
st.subheader("3. Here is how we read your quote")
if note := st.session_state.pop("vendor_confirm_note", None):
    st.success(note)
summary = questions.quote_summary(scope)
rows = summary["lines"]
priced = [r for r in rows if r["status"] in ("read", "confirmed by you")]
done_at = answers.last_confirmed(scope)
if done_at and not open_qs and priced and all(r["status"] == "confirmed by you" for r in priced):
    st.success(copy.VENDOR_DONE.format(when=copy.date(done_at)))
st.caption("Select the lines that are right (click the box at the start of a row), then "
           "confirm them. A line with an open question can be confirmed once you have "
           "answered it.")
picked = st.dataframe(
    [{"Line": r["line_no"], "Item": r["description"],
      "You wrote": r["as_quoted"] or "",
      "Price per unit": copy.unit_price(Decimal(r["price_per_unit"]))
      if r["price_per_unit"] else "", "Unit": r["unit"],
      "Status": STATUS_WORDS.get(r["status"], r["status"].capitalize()),
      "Waiting on": (r["blocked_by"] or "").capitalize()} for r in rows],
    hide_index=True, width="stretch", row_height=35, height=35 * (len(rows) + 1) + 3,
    on_select="rerun", selection_mode="multi-row",
    column_config={"Line": st.column_config.NumberColumn(width=50),
                   "Price per unit": st.column_config.TextColumn(
                       f"Price per unit ({summary['currency']})", alignment="right")},
    key=f"confirm_{len(open_qs)}_{sum(r['status'] == 'confirmed by you' for r in rows)}")
freight = str(summary["freight"])
freight = freight[len("freight "):] if freight.lower().startswith("freight ") else freight
st.caption(f"Freight: {freight}")

ticked = [rows[i] for i in picked.selection.rows]
waiting = [r for r in ticked if r["blocked_by"]]
if waiting:
    st.warning("; ".join(f"Line {r['line_no']} waits for your answer to {r['blocked_by']}"
                         for r in waiting) + ". Answer it in the chat above first.")
already = [r["line_no"] for r in ticked if r["status"] == "confirmed by you"]
if already:
    st.info(f"Already confirmed: line{'s' if len(already) > 1 else ''} "
            f"{', '.join(map(str, already))}.")
blocking = [q for q in open_qs if questions.blocks_confirm(q)]
c1, c2, c3 = st.columns(3)
ready = [r for r in ticked if r["can_confirm"]]
if c1.button(f"Confirm selected lines ({len(ready)})", type="primary", disabled=not ready):
    n = answers.confirm(scope, [r["rfx_line_id"] for r in ready])
    st.session_state["vendor_confirm_note"] = f"Thank you. {n} line{'s' if n != 1 else ''} " \
                                              "confirmed."
    st.rerun()
if c2.button("Confirm all my prices", disabled=bool(blocking),
             help="Answer the open questions first" if blocking else None):
    answers.confirm(scope)
    st.session_state["vendor_confirm_note"] = "Thank you. Your quote is confirmed."
    st.rerun()
if priced:
    with c3.popover("Something wrong?"):
        pick = st.selectbox("Which line?", priced,
                            format_func=lambda r: f"Line {r['line_no']}: {r['description']}")
        if st.button("This line is wrong"):
            answers.correct(scope, pick["rfx_line_id"])
            st.rerun()
