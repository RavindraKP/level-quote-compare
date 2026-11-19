"""Inbox (WP4, polished in WP12): response tracker, replies with live reading progress, the
emails sent, and Add a reply. Layout and calls only."""

import streamlit as st
from components import data, frame

from level import copy, sandbox
from level.agents.vendor_copilot.link import link_url
from level.types import CellState

rfx = data.current_rfx(st.session_state.get("rfx_id"))
frame.header(rfx, "Inbox")
if rfx is None:
    st.info("No event yet. Create one on the RFx page and send it to vendors; their replies "
            "appear here.")
    st.stop()
if rfx.get("is_sandbox") and rfx["status"] == "draft":
    st.info("Set your lines on the RFx page first, then press \"Start adding quotes\". Vendor "
            "quotes are added here after that.")
    if st.button("Go to the RFx page", type="primary"):
        st.switch_page("views/1_RFx.py")
    st.stop()
n_lines = len(data.lines(rfx["id"]))


def read_reply(message_id: int) -> None:
    """Queue a reading; in a sandbox the reading cap may say no (shown above the replies)."""
    try:
        data.retry_message(message_id)
    except sandbox.Limit as exc:
        st.session_state["read_limit"] = str(exc)


@st.dialog("Add a reply", width="large")
def add_reply_dialog() -> None:
    vendors = data.vendors(rfx["id"])
    options = [v["id"] for v in vendors] + ["new"]
    names = {v["id"]: v["name"] for v in vendors} | {"new": "A new vendor"}
    if rfx.get("is_sandbox"):
        used = sandbox.usage(rfx["id"])["files"]
        st.caption(f"{copy.SANDBOX_NOTE} Files used: {used} of {sandbox.MAX_FILES}, up to "
                   f"{sandbox.MAX_FILE_MB} MB each.")
    with st.form("add_reply", border=False):
        pick = st.selectbox("Vendor", options, format_func=names.get)
        new_name = st.text_input("New vendor's name (only for a new vendor)")
        files = st.file_uploader("Files: Excel, PDF, Word, a photo, an email",
                                 accept_multiple_files=True)
        body = st.text_area("Or paste the email text", height=150)
        if st.form_submit_button("Save and read the reply", type="primary"):
            try:
                uploads = [data.Upload(f.name, f.getvalue(), f.type) for f in files or []]
                data.add_reply(rfx["id"], None if pick == "new" else pick, new_name, uploads, body)
                st.rerun()  # closes the dialog; progress shows in the list
            except ValueError as exc:
                st.error(str(exc))


# --- Response tracker ---------------------------------------------------------------------
top = st.columns([4, 1], vertical_alignment="bottom")
top[0].subheader("Responses")
if top[1].button("Add a reply", type="primary", width="stretch"):
    add_reply_dialog()
CHANNEL = {"email": "Email", "reply_link": "Reply link", "upload": "Added by you"}


def unread(msgs: list[dict]) -> list[dict]:
    """Replies that were never queued for reading (H8: a fresh deploy starts like this)."""
    return [m for m in msgs if not data.jobs_for_message(m["id"])]


@st.fragment(run_every=2)
def tracker() -> None:
    """The tracker refreshes with the reading progress below (L2)."""
    st.dataframe([{k: v for k, v in r.items() if k != "vendor_id"}
                  for r in data.tracker(rfx["id"])],
                 hide_index=True, width="stretch",
                 column_config={"Open issues": st.column_config.NumberColumn(width="small"),
                                "Lines quoted": st.column_config.TextColumn(alignment="right")})


tracker()
tab_in, tab_out = st.tabs(["Replies", "Sent"])


def file_problem(m: dict) -> str | None:
    """The first file in a reply that could not be opened, with the reason."""
    bad = [a for a in m["attachments"] if a.get("read_error")]
    return f"{bad[0]['filename']}: {bad[0]['read_error']}" if bad else None


@st.fragment(run_every=2)
def replies() -> None:
    """Re-renders every 2 s on its own, reading job rows; the rest of the page stays still."""
    msgs = data.messages(rfx["id"], "in")
    if limit := st.session_state.pop("read_limit", None):
        st.warning(limit)
    if not msgs:
        st.info("No replies yet. Replies appear here as vendors respond; you can also add one "
                "yourself with Add a reply.")
        return
    waiting = unread(msgs)
    if waiting:
        note, button = st.columns([3, 1], vertical_alignment="center")
        note.info(f"{copy.plural(len(waiting), 'reply', 'replies')} not read yet. Reading "
                  "takes a few minutes and runs in the background; you can leave this page.")
        if button.button(f"Read all {len(waiting)}" if len(waiting) > 1 else "Read it now",
                         type="primary", width="stretch", key="read_all"):
            for m in waiting:
                read_reply(m["id"])
            st.rerun()
    for m in msgs:
        jobs = data.jobs_for_message(m["id"])
        job = jobs[-1] if jobs else None
        vendor = m["vendor_name"] or "Unknown vendor"
        with st.container(border=True):
            head, status = st.columns([3, 2])
            files = ", ".join(a["filename"] for a in m["attachments"]) or "message text only"
            channel = CHANNEL.get(m["channel"], m["channel"].replace("_", " ").capitalize())
            head.markdown(f"**{vendor}** · {channel} · "
                          f"{copy.date_time(m['received_at'])}  \n{files}")
            if job is None:
                status.warning("Not read yet.")
                if status.button("Read this reply", key=f"read_{m['id']}"):
                    read_reply(m["id"])
                    st.rerun()
            elif job.status in ("queued", "running"):
                status.progress(job.progress, text=copy.reading_stage(job.stage, vendor, n_lines))
            elif job.status == "failed":
                status.error(copy.read_failure(file_problem(m)))
                if status.button("Try again", key=f"retry_{m['id']}"):
                    read_reply(m["id"])
            else:
                f = data.reply_findings(rfx["id"], m)
                mins = data.minutes_to_grid(m, jobs)
                states = ", ".join(
                    f"{n} {data.STATE_LABEL[CellState(s)].lower()}"
                    for s, n in sorted(f["states"].items()))
                status.success(f"Read in {mins} min" if mins is not None else "Read")
                status.caption(f"{f['claims']} prices found · {states or 'no lines'} · "
                               f"{copy.plural(f['open_flags'], 'open issue')}")
                if problem := file_problem(m):
                    status.warning(copy.read_failure(problem))
                if status.button("Read again", key=f"reread_{m['id']}",
                                 help="Reads this reply again from the original files"):
                    read_reply(m["id"])
            if m["body"]:
                with st.expander("Message text"):
                    st.text(m["body"])


with tab_in:
    replies()

with tab_out:
    with st.expander("Vendor reply links (each opens only that vendor's own page)"):
        st.dataframe([{"Vendor": v["name"], "Reply link": link_url(rfx["id"], v["id"])}
                      for v in data.vendors(rfx["id"])], hide_index=True, width="stretch",
                     column_config={"Reply link": st.column_config.LinkColumn(
                         width="large", help="Click to open; select the cell to copy it.")})
    sent = data.messages(rfx["id"], "out")
    if not sent:
        st.info("Nothing sent yet. The RFx email and any questions to vendors appear here "
                "once sent.")
    for m in sent:
        label = "Draft" if m["status"] == "draft" else "Sent"
        with st.expander(f"{label} to {m['vendor_name'] or m['recipient']}: {m['subject']} · "
                         f"{copy.date_time(m['received_at'] or m['created_at'])}"):
            st.text(m["body"] or "")
