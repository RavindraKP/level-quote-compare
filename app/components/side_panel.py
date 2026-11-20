"""A detail panel beside a grid, opened by selecting a cell. Layout only.

With nothing selected the grid has the full width. With a selection the area splits about
2:1: grid on the left, the detail on the right. The panel column stays in view while the
page scrolls (CSS in style.py, matched on the `lv-panel` key prefix) and scrolls on its own
when it is taller than the window.
"""

from __future__ import annotations

from contextlib import contextmanager

import streamlit as st


def clear_selection(grid_key: str) -> None:
    """Unselect everything in one st.dataframe (programmatic selection, Streamlit 1.67)
    and forget what the panel was showing, which closes it."""
    st.session_state[grid_key] = {"selection": {"rows": [], "columns": [], "cells": []}}
    st.session_state.pop(f"lv-open-{grid_key}", None)


def keep_open(grid_key: str, raw, valid):
    """What the panel shows: the valid selection, or the last one while the person clicks
    a cell that has no detail (a line name, a total). Unselecting, or Close, closes it.

    raw: the grid's selection as it is; valid: the same, or None if it has no detail.
    Kept in session state because it is only where the panel is, not data."""
    remembered = f"lv-open-{grid_key}"
    if valid is not None:
        st.session_state[remembered] = valid
    elif raw is None:
        st.session_state.pop(remembered, None)
    return st.session_state.get(remembered)


def columns(is_open: bool):
    """Where the grid goes, and where the panel goes.

    The two columns always exist, so the grid keeps its place (and its sideways scroll)
    when the panel opens or closes. While closed, the panel column holds only a marker
    and CSS hides it, giving the grid the full width."""
    grid, panel = st.columns([2, 1], gap="medium")
    if not is_open:
        panel.html('<span class="lv-panel-closed"></span>')  # an empty container is not drawn
    return grid, panel


@contextmanager
def panel(where, name: str, title: str, grid_key: str, subtitle: str = ""):
    """The panel itself: a bordered box with a heading and a Close button.

    name: a stable, page-unique id for the panel (used for the CSS class and the button
    key). Close clears the grid's selection, which closes the panel on the rerun."""
    with where, st.container(border=True, key=f"lv-panel-{name}"):
        # Heading takes the room it needs; Close keeps its full label at any panel width.
        with st.container(horizontal=True, vertical_alignment="center", wrap=False):
            st.markdown(f"**{title}**", width="stretch")
            st.button("Close", key=f"lv-close-{name}", on_click=clear_selection,
                      args=(grid_key,), width="content",
                      help="Close this panel and unselect it in the list")
        if subtitle:
            st.caption(subtitle)
        yield
