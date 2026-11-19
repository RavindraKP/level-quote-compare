"""Entry point: `uv run streamlit run app/Home.py`. Gate, start-up, then navigation."""

import streamlit as st
from common import boot, passcode_ok
from components import data, style

from level import copy
from level.agents.vendor_copilot.link import verify_token

st.set_page_config(page_title="Level", layout="wide")
style.apply()

# A vendor's personal reply link (?t=...) opens only their own page: no passcode, no buyer
# navigation. The signed token decides which RFx and vendor the page may show.
if "t" in st.query_params:
    scope = verify_token(st.query_params["t"])
    if scope is None:
        st.error("This link is not valid. Please use the link from your RFx email.")
        st.stop()
    boot()
    st.session_state["vendor_scope"] = scope
    st.navigation([st.Page("views/8_Vendor_Reply.py", title="Your quote", default=True)],
                  position="hidden").run()
    st.stop()

if not passcode_ok():
    st.stop()

boot()

events = data.events()
if "rfx" in st.query_params and st.query_params["rfx"].isdigit():  # from a stepper link
    st.session_state["rfx_id"] = int(st.query_params["rfx"])
# The event the pages show is pinned for the whole session once chosen, so sending a new
# RFx never moves the buyer to a different, empty event (UX audit B10).
if st.session_state.get("rfx_id") not in {e["id"] for e in events} | {None}:
    st.session_state.pop("rfx_id")  # that event no longer exists (demo data reset)
if events and st.session_state.get("rfx_id") is None:
    picked = data.current_rfx(None)
    st.session_state["rfx_id"] = picked["id"] if picked else None
if len(events) > 1:  # view state only: which event the pages show
    with st.sidebar:
        chosen = st.selectbox("Event", events, format_func=copy.event_choice,
                              help="Which event every page shows.",
                              index=next((i for i, r in enumerate(events)
                                          if r["id"] == st.session_state.get("rfx_id")), 0))
        st.session_state["rfx_id"] = chosen["id"]

rfx = data.current_rfx(st.session_state.get("rfx_id"))
issues = data.open_issue_count(rfx["id"]) if rfx else 0
overview = st.Page("views/0_Overview.py", title="Overview", default=True)  # lives at "/"


def _to_overview() -> None:
    st.switch_page(overview)


pages = {
    # M8: the default page's own address is "/", so a bookmarked /overview forwards there
    # instead of showing "Page not found".
    "": [overview, st.Page(_to_overview, title="Overview", url_path="overview",
                           visibility="hidden")],
    "Sourcing": [
        st.Page("views/1_RFx.py", title="RFx", url_path="rfx"),
        st.Page("views/2_Inbox.py", title="Inbox", url_path="inbox"),
    ],
    "Evaluation": [
        st.Page("views/3_Review.py", title=f"Review ({issues} open)" if issues else "Review",
                url_path="review"),
        st.Page("views/4_Compare.py", title="Compare", url_path="compare"),
        st.Page("views/5_Analyst.py", title="Analyst", url_path="analyst"),
    ],
    "Decision": [st.Page("views/6_Award.py", title="Award", url_path="award")],
    "Admin": [st.Page("views/7_Evals.py", title="Evals", url_path="evals")],
}
st.navigation(pages).run()
