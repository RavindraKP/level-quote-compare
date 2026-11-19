"""Review: only the points that need a person. Layout and calls only.

Left: the issue list, one heading per vendor (full name, open count, money at stake) and one
plain line per issue naming the line and the value. Right: the focus card for the selected
issue: progress with Previous/Next, what we found, the vendor's own words, the price check
(buyer only), and actions that say what they do. After a decision the next open issue opens;
the last decision can be undone. Rules: docs/UX_GUIDE.md "Review queue".
"""

from html import escape

import streamlit as st
from components import data, frame, review_data, source_viewer, style

from level import copy
from level.core import review as core
from level.types import FlagType

rfx = data.current_rfx(st.session_state.get("rfx_id"))
frame.header(rfx, "Review")
if rfx is None:
    st.info("No event yet. Issues appear here once vendors' replies have been read.")
    st.stop()
rfx_id = rfx["id"]

items = review_data.issues(rfx_id)
open_items = [i for i in items if not i.decided]
last = data.last_decision(rfx_id)

if not items:
    o = data.overview(rfx_id)
    if o["replies"] == 0:
        st.info("No issues yet. Issues appear here once vendors reply and their quotes are "
                "read.")
    elif o["replies_read"] < o["replies"]:
        reading = any(j.status.value in ("queued", "running")
                      for m in data.messages(rfx_id, "in") for j in data.jobs_for_message(m["id"]))
        st.info("Replies are still being read. Issues appear here as they are found; follow "
                "the reading on the Inbox page." if reading else copy.REVIEW_NOT_READ)
        st.page_link("views/2_Inbox.py", label="Open the Inbox")
    else:
        st.success("Nothing needs you. Every point raised on the replies has been settled.")
    st.stop()

st.caption(copy.REVIEW_INTRO)


def undo() -> None:
    data.undo_last(rfx_id)
    st.toast(copy.REVIEW_UNDONE)


# --- The last decision, with Undo ------------------------------------------------------------
if last:
    by_id = {i.flag.id: i for i in items}
    first = by_id.get(last["flag_ids"][0])
    text = copy.last_decision_text(last["action"], len(last["flag_ids"]),
                                   first.line_no if first else None,
                                   first.vendor if first else "")
    bar, btn = st.columns([5, 1], vertical_alignment="center")
    bar.html(f'<div class="lv-note">{escape(text)}</div>')
    if btn.button(copy.REVIEW_UNDO, key="undo_last", width="stretch"):
        undo()
        if first:
            st.session_state["review_key"] = first.key
        st.rerun()

# --- Filters ---------------------------------------------------------------------------------
f1, f2, f3 = st.columns([1.2, 2, 1], vertical_alignment="bottom")
priorities = f1.multiselect(copy.REVIEW_FILTER_PRIORITY, list(copy.SEVERITY_LABEL.values()),
                            placeholder="All priorities", key="rv_priority")
titles = list(dict.fromkeys(i.title for i in items))
kinds = f2.multiselect(copy.REVIEW_FILTER_TYPE, titles, placeholder="All issue types",
                       key="rv_kind")
show_decided = f3.toggle(copy.REVIEW_SHOW_DECIDED, key="rv_decided")


def visible(i) -> bool:
    return ((not priorities or copy.SEVERITY_LABEL[i.flag.severity] in priorities)
            and (not kinds or i.title in kinds))


in_filter = [i for i in items if visible(i)]
shown = [i for i in in_filter if show_decided or not i.decided]
shown_keys = [i.key for i in shown]
open_keys = {i.key for i in in_filter if not i.decided}
if not shown:
    st.success(copy.REVIEW_NOTHING_LEFT if not open_items else copy.REVIEW_NONE_MATCH)
    st.stop()

# The selected issue: kept by a key that survives rebuilds; else the first open one.
current = st.session_state.get("review_key")
if current not in shown_keys:
    current = next((k for k in shown_keys if k in open_keys), shown_keys[0])
    st.session_state["review_key"] = current
issue = next(i for i in shown if i.key == current)


def pick(key: str) -> None:
    st.session_state["review_key"] = key


# --- Left: the issue list --------------------------------------------------------------------
left, right = st.columns([1, 1.9], gap="medium")
heads = review_data.vendor_headings(items)
with left, st.container(height=720, key="lv-queue", border=False):
    vendor_seen = None
    for i in shown:
        if i.flag.vendor_id != vendor_seen:
            vendor_seen = i.flag.vendor_id
            h = heads[vendor_seen]
            rest = copy.vendor_heading("", h["open"], h["stake"]).lstrip(" ·")
            st.html(f'<div class="lv-vhead">{escape(h["name"])}'
                    f'<span class="lv-dim">{escape(rest)}</span></div>')
        label = ("✓ " if i.decided else "") + i.summary
        st.button(label, key=f"pick_{i.key}", width="stretch",
                  type="primary" if i.key == current else "tertiary",
                  on_click=pick, args=(i.key,))


def after_decision() -> str | None:
    """The issue to open once this one is decided: the next open one in queue order."""
    return core.next_open(shown_keys, open_keys, issue.key)


def decide(action: str, value: str | None = None) -> None:
    nxt = after_decision()
    if not review_data.is_open(issue.flag.id):  # decided in another tab meanwhile (L3)
        st.session_state["review_key"] = nxt
        st.toast(copy.ALREADY_DECIDED)
        st.rerun()
    folded = [f.id for f in issue.folded]  # same line, same root: decided together (M9)
    try:
        if folded and action != "edit_value":
            data.resolve_flags([issue.flag.id, *folded], action)  # one batch, one undo
        else:
            data.resolve_flag(issue.flag.id, action, value,
                              st.session_state.get(f"note_{issue.key}") or None)
            if folded:
                data.resolve_flags(folded, "confirm")
    except ValueError as exc:
        st.error(str(exc))
        return
    st.session_state["review_key"] = nxt
    st.toast(copy.REVIEW_SAVED)
    st.rerun()


def confirm_bulk(group: list) -> None:
    """The lines and values to be accepted, shown before anything is saved."""
    st.html('<table class="lv-ctx">' + "".join(
        f'<tr><td>Line {g.line_no}</td><td>{escape(g.line_desc or "")}</td>'
        f'<td class="n">{copy.unit_price(g.price)} per {escape(g.uom or "")}</td></tr>'
        for g in group) + "</table>")
    st.caption(f"Each stays {copy.STATE_LABEL[data.CellState.INFERRED].lower()} in the "
               "comparison until the vendor confirms it. You can undo this in one click.")
    if st.button(copy.bulk_confirm(len(group)), type="primary", key="bulk_ok"):
        nxt = core.next_open(shown_keys, open_keys - {g.key for g in group}, issue.key)
        data.resolve_flags([g.flag.id for g in group], "confirm")
        st.session_state["review_key"] = nxt
        st.rerun()


# --- Right: the focus card -------------------------------------------------------------------
with right:
    all_keys = [i.key for i in in_filter]
    pos, total = core.position(all_keys, issue.key)
    p1, p2, p3 = st.columns([3, 1, 1], vertical_alignment="center")
    p1.markdown(f"**{copy.progress(pos, total, len(open_keys))}**")
    prev_key = core.step(shown_keys, issue.key, -1)
    next_key = core.step(shown_keys, issue.key, +1)
    p2.button(copy.REVIEW_PREV, key="rv_prev", width="stretch", disabled=prev_key is None,
              on_click=pick, args=(prev_key,))
    p3.button(copy.REVIEW_NEXT, key="rv_next", width="stretch", disabled=next_key is None,
              on_click=pick, args=(next_key,))

    with st.container(border=True):
        where = f"Line {issue.line_no} · {issue.line_desc}" if issue.line_no else ""
        tone = {"critical": "red", "warning": "amber"}.get(issue.flag.severity.value, "grey")
        stake = copy.stake_text(issue.stake)
        st.html(
            f'<div class="lv-card-head"><span class="lv-issue-title">{escape(issue.title)}'
            f'</span>{style.chip(copy.SEVERITY_LABEL[issue.flag.severity] + " priority", tone)}'
            + (style.chip("Decided", "teal") if issue.decided else "") + "</div>"
            f'<div class="lv-dim">{escape(issue.vendor)}'
            + (f" · {escape(where)}" if where else "")
            + (f" · {escape(stake)}" if stake else "") + "</div>")

        st.markdown(f"**{copy.REVIEW_FOUND}:** {issue.found}")
        st.caption(f"{copy.REVIEW_WHY}: {issue.why or copy.ISSUE_WHY[issue.flag.type]}")

        ev_col, ctx_col = st.columns([1.15, 1], gap="medium") if issue.is_price else (
            st.container(), None)
        with ev_col:
            st.markdown(f"**{copy.REVIEW_EVIDENCE}**")
            src = review_data.source(issue)
            ev = src.evidence
            if ev and ev.quote:
                st.html(f'<div class="lv-dim">{copy.REVIEW_VENDOR_WROTE}</div>'
                        f'<div class="lv-quote">“{escape(ev.quote)}”</div>'
                        + (f'<div class="lv-dim">{escape(copy.plain(ev.locator))}</div>'
                           if ev.locator else ""))
            elif issue.flag.type == FlagType.READ_MISMATCH and issue.flag.candidates:
                st.markdown(f"Hard to read: **{' or '.join(issue.flag.candidates)}**?")
            elif src.file_exists or src.email_body:
                st.caption(copy.REVIEW_WHOLE_REPLY)
            else:
                st.caption(copy.REVIEW_NO_WORDS)
            if src.file_exists:
                with st.expander("Show it in their document", expanded=not issue.is_price):
                    source_viewer.show(ev, key=f"rv_{issue.key}", show_quote=False)
            elif src.email_body:
                with st.expander(copy.REVIEW_WHOLE_EMAIL):
                    source_viewer.show_message_body(src.email_body, ev.quote if ev else None)

        if ctx_col is not None:
            with ctx_col:
                ctx = review_data.price_context(rfx_id, issue)
                st.markdown(f"**{copy.PRICE_CONTEXT}** "
                            f"<span class='lv-dim'>{copy.PRICE_CONTEXT_NOTE}</span>",
                            unsafe_allow_html=True)
                vs = (f" (this price is {ctx['vs_median']}×)"
                      if ctx["vs_median"] is not None else "")
                rows = [
                    ("This vendor, last year", ctx["own"] or
                     "No record: they did not supply or quote this line last year"),
                    ("Median of other quotes",
                     (ctx["median"] + vs) if ctx["median"] else "No other quotes yet"),
                    ("Last year's contract", ctx["last_contract"] or "No contract record"),
                ]
                peers = "".join(
                    f'<tr><td class="k">{escape(p["Vendor"])}</td>'
                    f'<td class="n">{p[f"Price per {issue.uom}"]}</td>'
                    f'<td>{escape(p["Status"])}</td></tr>' for p in ctx["peers"])
                st.html(
                    '<table class="lv-ctx">' + "".join(
                        f'<tr><td class="k">{escape(k)}</td><td colspan="2">{escape(v)}</td>'
                        "</tr>" for k, v in rows)
                    + '<tr><td class="k" colspan="3"><b>Other vendors this year, per '
                    f'{escape(issue.uom or "unit")}</b></td></tr>'
                    + (peers or '<tr><td colspan="3">No other quotes yet</td></tr>')
                    + "</table>")

    # --- Actions ------------------------------------------------------------------------------
    if issue.decided:
        d = issue.decision
        st.info(copy.decided_text(d.get("action"), issue.flag.closed_by.value
                                  if issue.flag.closed_by else None, d.get("value")))
        if last and issue.flag.id in last["flag_ids"] and st.button(copy.REVIEW_UNDO,
                                                                     key="undo_here"):
            undo()
            st.rerun()
        st.stop()

    group = review_data.same_cause(items, issue)
    if group:
        b1, b2 = st.columns([3, 1], vertical_alignment="center")
        b1.markdown(copy.bulk_offer(len(group), issue.vendor, issue.title))
        if b2.button(copy.bulk_button(len(group)), key="bulk", width="stretch"):
            st.dialog(copy.bulk_title(len(group), issue.vendor), width="medium")(
                confirm_bulk)(group)

    st.markdown(f"**{copy.REVIEW_DECIDE}**")
    st.text_input(copy.REVIEW_NOTE, key=f"note_{issue.key}")  # typed before deciding
    on_line = issue.is_price
    # A price far from the other quotes is usually a unit slip: entering the right price or
    # asking the vendor is the main action, not accepting it as read (UX audit M9).
    outlier = review_data.is_outlier(issue)
    a1, a2 = st.columns(2)
    with a1:
        if st.button(copy.accept_label(issue.flag.type, issue.price, on_line,
                                       issue.history_source, issue.flag.reason),
                     type="secondary" if outlier else "primary", width="stretch",
                     key=f"ok_{issue.key}"):
            decide("confirm")
        for c in issue.flag.candidates if on_line else []:
            if st.button(copy.use_candidate_label(c), width="stretch",
                         key=f"use_{issue.key}_{c}"):
                decide("edit_value", str(c))
        if on_line and st.button(copy.not_quoted_label(issue.line_no), width="stretch",
                                 key=f"nq_{issue.key}"):
            decide("not_quoted")
    with a2:
        if on_line:
            with st.popover(copy.edit_label(), width="stretch",
                            type="primary" if outlier else "secondary"):
                new = st.text_input(copy.PRICE_EDIT_FIELD.format(uom=issue.uom or "unit"),
                                    key=f"val_{issue.key}", placeholder="e.g. 12.50")
                if st.button(copy.save_price_label(issue.line_no), type="primary",
                             key=f"save_{issue.key}"):
                    decide("edit_value", new)
        ask = issue.vendor_can_answer and st.button(
            copy.ask_label(issue.vendor), width="stretch", key=f"ask_{issue.key}")

    # --- Ask the vendor: the drafted email, inline -------------------------------------------
    if not issue.vendor_can_answer:
        st.caption(copy.REVIEW_ASK_NOT_FOR_VENDOR)
    else:
        drafts = data.outbox_drafts(rfx_id, issue.flag.vendor_id)
        if ask and not drafts:
            with st.spinner(f"Writing the email to {issue.vendor}: grouping their open "
                            "points…"):
                data.draft_for_vendor(rfx_id, issue.flag.vendor_id)
            drafts = data.outbox_drafts(rfx_id, issue.flag.vendor_id)
        if drafts:
            n = review_data.question_number(rfx_id, issue.flag.vendor_id, issue.flag.id)
            where_q = (f"This point is question {n} in the email." if n else
                       "This point waits for the next email (at most five questions each).")
            with st.expander(f"Email to {issue.vendor} · {where_q}", expanded=bool(ask)):
                d = drafts[0]
                subject = st.text_input("Subject", d["subject"], key=f"subj_{d['id']}")
                body = st.text_area("Email", d["body"], height=300, key=f"body_{d['id']}")
                st.caption(copy.REVIEW_EMAIL_OPEN)
                if st.button(copy.REVIEW_SEND, type="primary", key=f"send_{d['id']}"):
                    data.send_draft(d["id"], subject, body)
                    st.toast(f"Sent to {issue.vendor} (simulated). A copy is in the Inbox "
                             "under Sent.")
                    st.rerun()
        else:
            st.caption(copy.REVIEW_ASK_INTRO)
