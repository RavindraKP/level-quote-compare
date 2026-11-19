"""The frame every buyer page shares: event header and stage stepper. Layout only.

Header: event name and number, status chip, response deadline, buyer.
Stepper: Draft → Issued → Collecting → Evaluate → Awarded; each stage links to its page.
"""

from __future__ import annotations

from html import escape

import streamlit as st

from components import data, style
from level import copy

# Which page each stage opens (the url paths set in Home.py).
STAGE_PAGE = {"Draft": "rfx", "Issued": "inbox", "Collecting": "inbox", "Evaluate": "compare",
              "Awarded": "award"}
STATUS_TONE = {"Draft": "grey", "Issued": "teal", "Collecting": "teal", "Evaluate": "amber",
               "Awarded": "teal"}


def header(rfx: dict | None, page: str) -> None:
    """Event header, stepper and the page title. With no event, only the title."""
    if rfx is None:
        st.title(page)
        return
    invited = data.vendors(rfx["id"]) if rfx["status"] in ("issued", "collecting") else []
    # M7: once the reply date has passed or everyone replied, the event is being evaluated.
    stage = copy.event_stage(rfx, all_replied=bool(invited) and all(v["replied"]
                                                                     for v in invited))
    due = (rfx.get("terms") or {}).get("due")
    company = copy.buyer_company(rfx)
    meta = [copy.event_number(rfx), copy.deadline(due),
            f"Buyer: {copy.buyer_name()}" + (f", {company}" if company else "")]
    st.html(
        f'<div class="lv-head"><span class="lv-title">{escape(rfx["title"])}</span>'
        f"{style.chip(stage, STATUS_TONE[stage])}"
        + (style.chip(copy.SANDBOX_CHIP, "amber") if rfx.get("is_sandbox") else "")
        + "".join(f'<span class="lv-meta">{escape(m)}</span>' for m in meta)
        + "</div>" + stepper(stage, rfx["id"]))
    if rfx.get("is_sandbox"):  # on every page of a sandbox, so it is never taken for the demo
        st.caption(copy.SANDBOX_NOTE)
    st.title(page)


def stepper(stage: str, rfx_id: int) -> str:
    """HTML for the stage stepper; stages before the current one are marked done."""
    now = copy.STAGES.index(stage)
    parts = []
    for i, name in enumerate(copy.STAGES):
        cls = "now" if i == now else "done" if i < now else ""
        mark = "✓ " if i < now else ""
        parts.append(f'<a class="lv-step {cls}" target="_self" '
                     f'href="./{STAGE_PAGE[name]}?rfx={rfx_id}">{mark}{name}</a>')
    return '<div class="lv-steps">' + '<span class="lv-arrow">→</span>'.join(parts) + "</div>"
