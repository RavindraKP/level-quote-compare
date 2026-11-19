"""Overview (home): event counters (WP4) and the worker self-test (WP0).

The self-test proves the background worker: click Start, refresh, double-click, open a
second tab; the job runs once and the page stays responsive.
"""

import os

import streamlit as st
from components import data, frame, style

from level import copy, demo, worker

rfx = data.current_rfx(st.session_state.get("rfx_id"))
frame.header(rfx, "Overview")
if rfx is None:
    st.info("No event yet. Start one on the RFx page; it appears here once it is sent to "
            "vendors.")
else:
    # H8: on a fresh start the replies are stored but not read yet; say so and offer it here.
    waiting = [m for m in data.messages(rfx["id"], "in") if not data.jobs_for_message(m["id"])]
    if waiting:
        note, button = st.columns([3, 1], vertical_alignment="center")
        note.info(f"{copy.plural(len(waiting), 'reply', 'replies')} not read yet. Reading "
                  "runs in the background; follow it on the Inbox page.")
        if button.button("Read all replies", type="primary", width="stretch"):
            for m in waiting:
                data.retry_message(m["id"])
            st.switch_page("views/2_Inbox.py")
    o = data.overview(rfx["id"])
    times = [t["minutes"] for t in o["timings"]]
    by = o["closed_by"]
    style.tiles([
        ("Replies read", f"{o['replies_read']} of {o['replies']}",
         f"{o['replied']} of {o['invited']} invited vendors replied"),
        ("Arrival to comparison", f"{max(times)} min" if times else "—",
         "slowest reply, from arrival to the grid"),
        ("Points that needed you", str(o["needed_human"]),
         f"{o['open_flags']} still open"),
        ("Points settled", str(sum(by.values())),
         f"checks {by['checks']} · you {by['buyer']} · vendors {by['vendor']}"),
    ])
    st.subheader("Responses")
    tracked = data.tracker(rfx["id"])
    st.dataframe([{k: v for k, v in r.items() if k != "vendor_id"} for r in tracked],
                 hide_index=True, width="stretch",
                 column_config={"Open issues": st.column_config.NumberColumn(width="small"),
                                "Lines quoted": st.column_config.TextColumn(alignment="right")})
    if o["timings"]:
        with st.expander("Minutes from arrival to comparison, per reply"):
            st.dataframe([{"Vendor": t["vendor"], "Minutes": t["minutes"]}
                          for t in o["timings"]], hide_index=True)

st.divider()
st.caption("Administration")
# The worker self-test is for builders; the buyer never sees it (QA L10).
if os.environ.get("LEVEL_SHOW_SELFTEST"):
    with st.expander("Background reader test", expanded=False):
        st.caption("Queues a 20-second task. Double-clicks and refreshes return the same task.")
        if st.button("Start a 20-second task"):
            job_id = worker.enqueue("demo.sleep", "20", file_hash="selftest")
            st.query_params["job"] = str(job_id)  # in the URL, so a refresh or new tab keeps it

        @st.fragment(run_every=1)
        def job_status() -> None:
            # Only this fragment re-renders every second; the rest of the page stays still.
            job_id = st.query_params.get("job")
            if not job_id or not job_id.isdigit():
                st.write("No task started in this session.")
                return
            job = worker.get_job(int(job_id))
            if job is None:
                st.write(f"Task {job_id} is no longer on record. The demo data may have "
                         "been reset.")
                return
            st.progress(job.progress, text=f"Task {job.id}: {job.status}, {job.stage or 'waiting'}")
            st.caption(f"Attempts {job.attempts} · last seen {copy.date_time(job.heartbeat_at)}")
            if job.error:
                st.error("The task stopped. It is logged; start it again to retry.")

        job_status()


with st.expander("Demo data", expanded=False):
    # WP8: put the rehearsed event back in seconds. Logic lives in level/demo.py.
    if "demo_msg" in st.session_state:  # result of the last action, shown after the rerun
        st.success(st.session_state.pop("demo_msg"))
    saved = demo.info()
    st.caption(f"Saved copy: {copy.date_time(saved['saved_at'])} · {saved['messages']} "
               f"replies · {saved['cells']} prices" if saved else
               "No saved copy yet. Read the replies, then save the current data here.")
    confirm = st.text_input("Type RESET to enable these actions", key="demo_confirm")
    armed = confirm.strip() == "RESET"
    c = st.columns(3)
    try:
        if c[0].button("Restore saved copy", disabled=not armed or not saved):
            demo.restore()
            st.session_state["demo_msg"] = "Saved copy restored."
        elif c[1].button("Reload and read every reply", disabled=not armed,
                         help="Empties the data, loads the event again and reads every reply "
                              "(a few minutes; uses API credit)."):
            n = len(demo.reseed())
            st.session_state["demo_msg"] = (f"Reloaded. {n} replies are being read; follow them "
                                            "on the Inbox page.")
        elif c[2].button("Save current data as the copy", disabled=not armed):
            demo.save()
            st.session_state["demo_msg"] = "Current data saved as the copy."
        if "demo_msg" in st.session_state:
            st.session_state.pop("rfx_id", None)  # the event id may have changed
            st.session_state.pop("demo_confirm", None)  # disarm again
            st.rerun()  # redraw the counters above from the new database
    except (demo.Busy, FileNotFoundError) as e:
        st.warning(str(e))
