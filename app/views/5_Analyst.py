"""Analyst (WP6, polished in WP12): plain-language questions over the comparison.
Layout and calls only.

The thread lives in SQLite and is re-rendered from it on every run; session state holds only
which thread is open. A question is saved at once and answered by the background worker, so
an answer is never lost when the buyer leaves the page, and a second question cannot cut the
first one off (UX audit H5). Each answer shows the text first, then any table or chart, then
the caveats from the calculation (T10) in a muted box, then a collapsed "How this was worked
out" with one plain line per step (T13).
"""

from html import escape

import pandas as pd
import streamlit as st
from components import data, frame, style

from level import copy, sandbox, worker
from level.agents.analyst import agent, award, steps, tools
from level.types import JobStatus

rfx = data.current_rfx(st.session_state.get("rfx_id"))
frame.header(rfx, "Analyst")
if rfx is None:
    st.info("No event yet. Once vendors' replies are read, ask questions about them here.")
    st.stop()

THREAD_KEY = f"analyst_thread_{rfx['id']}"  # one open conversation per event
if THREAD_KEY not in st.session_state:
    st.session_state[THREAD_KEY] = agent.latest_thread(rfx["id"])
thread_id = st.session_state[THREAD_KEY]
AVATAR = {"user": ":material/person:", "assistant": ":material/query_stats:"}  # on-theme (L1)

top = st.columns([4, 1], vertical_alignment="center")
top[0].caption("Answers use only figures from the quotes and the award calculator. Nothing "
               "you ask here changes a price.")
if top[1].button("New conversation", width="stretch"):
    st.session_state[THREAD_KEY] = agent.new_thread(rfx["id"])
    st.rerun()

vendors = tools._vendors(rfx["id"])


def words(text: str) -> str:
    """Model text as the buyer reads it: internal words swapped, Indian digit grouping."""
    return copy.indian_numbers(copy.plain(text))


def show_artefact(a: dict, key: str) -> None:
    if a["kind"] == "table":
        st.markdown(f"**{a['title']}**")
        frame_ = pd.DataFrame(a["rows"])
        st.dataframe(frame_.rename(columns=copy.column_label), hide_index=True, width="stretch")
    elif a["kind"] == "chart":
        st.vega_lite_chart(a["spec"], width="stretch")
    elif a["kind"] == "file":
        try:
            with open(a["path"], "rb") as fh:
                st.download_button(f"Download {a['title']}", fh.read(), file_name=a["title"],
                                   key=f"dl_{key}")
        except FileNotFoundError:
            st.caption(f"{a['title']}: the file is no longer on the server. Ask again to make "
                       "it.")
    elif a["kind"] == "email":
        with st.expander(f"Draft email: {a['title']} (in the outbox, not sent)"):
            st.text(a["body"])


def show_caveats(caveats: list[str]) -> None:
    """At most five caveats that change the number, the basis in one muted line, and the
    vendors' terms folded away (UX audit M2)."""
    parts = copy.caveat_parts(caveats)
    if parts["main"] or parts["basis"]:
        style.note("<b>Caveats from the calculation</b><br>"
                   + "<br>".join(f"· {escape(c)}" for c in parts["main"])
                   + (f"<br><span class='lv-dim'>{escape(' '.join(parts['basis']))}</span>"
                      if parts["basis"] else ""))
    folded = [(f"More caveats ({len(parts['more'])})", parts["more"]),
              (copy.terms_noted(len(parts["terms"])), parts["terms"])]
    for title, items in folded:
        if items:
            with st.expander(title):
                st.markdown("\n".join(f"- {c}" for c in items))


def show_answer(m: dict) -> None:
    c = m["content"]
    if c.get("ungrounded"):
        st.warning("Check these figures against the comparison before relying on them: "
                   f"{', '.join(c['ungrounded'])}.")
    st.markdown(words(c.get("text", "")))  # display wording only; stored text unchanged
    for i, a in enumerate(c.get("artefacts", [])):
        show_artefact(a, f"{m['id']}_{i}")
    if c.get("caveats"):
        show_caveats(c["caveats"])
    for sid in c.get("scenario_ids", []):
        name = copy.plain(award.option_name(rfx["id"], sid))  # the option in words, no id
        if st.button(f"Save “{name}” for the Award page", key=f"pin_{m['id']}_{sid}"):
            award.set_pinned(sid, True)
            st.toast(f"“{name}” saved for the Award page")
    calls = agent.tool_calls(m["id"])
    if calls:
        with st.expander("How this was worked out"):
            if c.get("retried"):
                st.caption("The first draft quoted a figure not found in the data; it was "
                           "rewritten once.")
            st.markdown("\n".join(f"{i}. {steps.describe(t, vendors)}"
                                  for i, t in enumerate(calls, start=1)))


def failure_text(error: str | None) -> str:
    """Why an answer could not be worked out, without the error's machinery."""
    if error and error.startswith("MissingAPIKey"):
        return ("The analyst is not available: this app has no API key set. Add "
                "LEVEL_ANTHROPIC_API_KEY to the app's settings and reload the page.")
    if error and error.startswith("BadAPIKey"):
        return ("The analyst is not available: the API key in the app's settings was refused. "
                "Check LEVEL_ANTHROPIC_API_KEY and reload the page.")
    return "The analyst could not finish this answer. Ask again, or put it another way."


history = agent.messages(thread_id)
jobs = agent.jobs_for_thread(thread_id)
running = agent.busy(thread_id)


@st.fragment(run_every=2)
def waiting(job_id: int) -> None:
    """While the worker answers, name the step; reload the thread when it is done."""
    job = worker.get_job(job_id)
    if job is None or job.status in (JobStatus.DONE, JobStatus.FAILED):
        st.rerun(scope="app")
    stage = job.stage if job and job.stage else ""
    text = "Reading the question" if not stage or stage == "Reading the question" else \
        steps.label(stage)
    st.status(f"{text}…", state="running", expanded=False)


for i, m in enumerate(history):
    with st.chat_message(m["role"], avatar=AVATAR[m["role"]]):
        if m["role"] == "user":
            st.markdown(m["content"].get("text", ""))
        else:
            show_answer(m)
    if m["role"] != "user" or (i + 1 < len(history) and history[i + 1]["role"] == "assistant"):
        continue
    # A question with no answer after it: being answered, failed, or lost.
    job = jobs.get(m["id"])
    with st.chat_message("assistant", avatar=AVATAR["assistant"]):
        if job and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            waiting(job.id)
        else:
            st.caption((failure_text(job.error) + " ") if job and job.status == JobStatus.FAILED
                       else "Not answered. ")
            if running is None and st.button("Ask again", key=f"again_{m['id']}"):
                agent.queue_answer(rfx["id"], thread_id, m["id"])
                st.rerun()

SUGGESTIONS = ["Summarise the replies received and the issues still open.",
               "Which vendor offers the lowest price on the most lines?",
               "Chart each vendor's total for the lines they quoted."]
picked = None
if not history:
    st.caption("Ask in your own words, or start with one of these:")
    picked = st.pills("Suggested questions", SUGGESTIONS, key=f"suggestion_{thread_id}",
                      label_visibility="collapsed")

placeholder = ("Wait for this answer before asking the next question…" if running else
               "Ask about the quotes, a split award, a what-if…")
question = st.chat_input(placeholder, disabled=running is not None) or picked
if question and running is None:
    try:
        sandbox.check_chat(rfx["id"])  # caps model use in a sandbox
    except sandbox.Limit as exc:
        st.warning(str(exc))
        st.stop()
    qid = agent.save_question(thread_id, question)
    agent.queue_answer(rfx["id"], thread_id, qid)
    st.rerun()  # the thread re-renders from SQLite with the question and a working line
