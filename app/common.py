"""Shared page helpers: start-up, passcode gate. Layout and calls only; logic lives in level/."""

from __future__ import annotations

import hmac
import logging
import os

import streamlit as st
from dotenv import load_dotenv

from level import db, worker

load_dotenv()  # .env for local runs; in the cloud the environment's settings are used
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


@st.cache_resource
def boot() -> str:
    """Once per server process: make sure the schema exists and the worker thread runs."""
    db.init()
    import components.backend  # noqa: F401  registers UI-side job handlers before the worker runs

    return worker.start().name


def passcode_ok() -> bool:
    """Gate the app with LEVEL_PASSCODE. If it is unset (local dev), the app is open."""
    expected = os.environ.get("LEVEL_PASSCODE")
    if not expected or st.session_state.get("authenticated"):
        return True
    st.title("Level")
    entered = st.text_input("Passcode", type="password")
    if entered:
        if hmac.compare_digest(entered, expected):
            st.session_state["authenticated"] = True
            st.rerun()
        st.error("Wrong passcode.")
    return False
