"""Evals (owned by WP7). Layout and calls only: the scorecards come from evals/report.py.

The page is read by examiners as well as builders, so headings are plain words (UX audit
M18); the command to run the evals is a caption for builders at the bottom.
"""

import pandas as pd
import streamlit as st
from components import data, frame

from evals import report
from level import copy

# The scorecard's metric names, in the words the rest of the app uses.
METRIC_WORDS = {
    "Silent errors (wrong and confirmed)": "Wrong prices shown as verified",
    "Cells correct or correctly flagged": "Prices read correctly, or sent to you to check",
    "Landed prices correct or correctly flagged":
        "Delivered prices correct, or sent to you to check",
    "  of which silent (wrong landed, Confirmed)": "  of which shown as verified",
    "Trap recall": "Tricky cases caught",
    "Flag precision": "Points raised that were real",
    "Vendor-term recall": "Vendor terms found",
    "Analyst correctness": "Analyst answers correct",
    "Paraphrase agreement": "Same answer to a reworded question",
    "Held-out questions": "Questions not seen while building",
    "Unanswerable declined": "Questions the data cannot answer, declined",
    "Injection ignored and flagged": "Instructions hidden in a quote ignored",
    "Minutes from reply to grid (avg)": "Minutes from reply to comparison (average)",
}
SUITE_WORDS = {"extraction": "Prices read", "traps": "Tricky cases",
               "analyst": "Analyst questions", "injection": "Instructions hidden in quotes"}
TIMING_WORDS = {"replies_timed": "Replies timed",
                "minutes_to_grid_avg": "Minutes from reply to comparison (average)",
                "minutes_to_grid_max": "Minutes from reply to comparison (slowest)",
                "flags_total": "Points raised", "flags_open": "Points still open",
                "human_touches": "Points that needed a person"}


def _value(text: str) -> str:
    return {"True": "Yes", "False": "No", "None": "—", "not built": "Not built"}.get(text, text)


frame.header(data.current_rfx(st.session_state.get("rfx_id")), "Evals")
st.caption("How well Level reads, matches and answers, scored against a hand-checked answer "
           "key for the demo data.")

runs = report.latest_runs()
if not runs:
    st.info("No scorecard yet. A builder runs the evals once; the scorecard then appears here.")
    st.stop()

label = st.selectbox("Data set", list(runs), index=0)
result = runs[label]
if result.get("fake"):
    st.warning("This scorecard is for the stand-in system used to test the scoring, "
               "not for Level itself.")
st.write(f"Scored on {copy.date_time(result['started_at'] + 'Z')}")

st.subheader("Scorecard")
st.dataframe(pd.DataFrame([(METRIC_WORDS.get(m, m), _value(v), t)
                           for m, v, t in report.headline(result)],
                          columns=["Measure", "Result", "Target"]),
             hide_index=True, width="stretch")

failures = report.failure_rows(result)
st.subheader(f"Cases that did not pass ({len(failures)})")
if not failures:
    st.success("Every case passed.")
for suite in ("extraction", "traps", "analyst", "injection"):
    rows = [f for f in failures if f["suite"] == suite]
    if rows:
        with st.expander(f"{SUITE_WORDS[suite]}: {len(rows)}", expanded=suite == "extraction"):
            st.caption("Each row names the vendor and line, or the test case, and what was "
                       "expected.")
            st.dataframe(pd.DataFrame(rows)[["link", "what"]].rename(
                columns={"link": "Where", "what": "What happened"}), hide_index=True,
                width="stretch")

suites = result["suites"]
with st.expander("Tricky cases, one row each"):
    if "results" in suites.get("traps", {}):
        st.dataframe(pd.DataFrame(suites["traps"]["results"])[
            ["id", "vendor", "status", "expected", "lines"]].astype(str).rename(columns={
                "id": "Case", "vendor": "Vendor", "status": "Result", "expected": "Expected",
                "lines": "Lines"}), hide_index=True, width="stretch")
    else:
        st.write(suites.get("traps", {}).get("detail", "Not run."))
with st.expander("Analyst questions and answers"):
    if "results" in suites.get("analyst", {}):
        st.dataframe(pd.DataFrame(suites["analyst"]["results"])[
            ["id", "set", "pass", "text", "expected", "answer"]].astype(str).rename(columns={
                "id": "Case", "set": "Set", "pass": "Passed", "text": "Question",
                "expected": "Expected", "answer": "Answer"}), hide_index=True, width="stretch")
    else:
        st.write(suites.get("analyst", {}).get("detail", "Not run."))
with st.expander("Time to comparison and who settled each point"):
    timing = suites.get("timing", {})
    rows = [(TIMING_WORDS[k], _value(str(timing[k]))) for k in TIMING_WORDS if k in timing]
    by = timing.get("closed_by") or {}
    if by:
        rows.append(("Settled by checks · you · vendors",
                     f"{by.get('checks', 0)} · {by.get('buyer', 0)} · {by.get('vendor', 0)}"))
    st.dataframe(pd.DataFrame(rows, columns=["Measure", "Result"]), hide_index=True,
                 width="stretch")

st.caption(f"For builders: run \"uv run python -m evals.run --seed A\" to score again. Model "
           f"{result.get('model')}, file {result['_file']}.")
