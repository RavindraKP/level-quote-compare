"""Compare (WP4, polished in WP12). Layout and calls only; data comes from components.data.

Bid summary tiles, then two tabs: Commercial (the price grid) and Technical (questionnaire
and documents). In the grid each cell shows the price, its L1/L2/L3 rank among eligible
prices and a status symbol; the lowest eligible price is bold; vendors that do not meet a
requirement are greyed with the reason in the column header.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape

import pandas as pd
import streamlit as st
from components import data, frame, receipt, side_panel, source_viewer, style

from level import copy
from level.types import CellState, Evidence

rfx = data.current_rfx(st.session_state.get("rfx_id"))
frame.header(rfx, "Compare")
if rfx is None:
    st.info("No event yet. Create one on the RFx page; vendors' prices appear here as their "
            "replies are read.")
    st.stop()

# --- Sidebar: assumptions (T7). Editing one recomputes every price. -----------------------
if note := st.session_state.pop("assumption_toast", None):
    st.toast(note)  # set before the rerun that refreshes the prices
with st.sidebar:
    st.subheader("Assumptions")
    st.caption("Used in every conversion. Changing one updates every price.")
    PERCENT = {"gst_rate", "cost_of_capital"}  # stored as a share, shown as a percentage (M5)
    for a in data.assumptions():
        label = copy.assumption_label(a["key"])
        is_pct = a["key"] in PERCENT
        with st.form(f"assumption_{a['key']}", border=True):
            shown = copy.assumption_value(a["key"], a["value"])
            st.markdown(f"**{label}**: {shown}")
            raw = copy.pct(Decimal(a["value"]) * 100).rstrip("%") if is_pct else a["value"]
            value = st.text_input("New value" + (" (%)" if is_pct else ""), raw,
                                  key=f"val_{a['key']}")
            source = st.text_input("Source", a["source"] or "", key=f"src_{a['key']}")
            try:
                when = date.fromisoformat((a["as_of"] or "")[:10])
            except ValueError:
                when = None
            as_of = st.date_input("As of", when, format="DD/MM/YYYY", key=f"asof_{a['key']}")
            if st.form_submit_button("Save and update prices"):
                try:
                    stored = str(Decimal(value) / 100) if is_pct else value
                    data.set_assumption(a["key"], stored, source,
                                        as_of.isoformat() if as_of else None, rfx["id"])
                    st.session_state["assumption_toast"] = (f"{label} updated; every price "
                                                            "recalculated")
                    st.rerun()
                except (ValueError, InvalidOperation):
                    st.error(f"{label}: enter a number, for example "
                             f"{'18' if is_pct else '86.50'}.")

grid = data.load_grid(rfx["id"])
if not grid.vendors:
    st.info("No vendors invited yet. Invite vendors on the RFx page; their columns appear "
            "here.")
    st.stop()

VIEWS = {"unit": "Price per unit", "landed": "Delivered price", "as_quoted": "As quoted"}
BASIS = {"unit": "₹ per unit of each line, ex-GST, ex-works",
         "landed": "₹ per unit of each line, ex-GST, delivered to the plant (freight included)",
         "as_quoted": "the vendor's own currency and unit, exactly as written"}
# The price-basis control sits with the price comparison below; the tiles use its value from
# the last run (Streamlit keeps a widget's value in session state under its key).
view = st.session_state.get("view") or "unit"
summary = data.vendor_summary(grid, view)
ranks = data.ranks_by_line(grid, view)
reasons = data.gating_reasons(rfx["id"])
tracked = {r["vendor_id"]: r for r in data.tracker(rfx["id"])}
failed = {v["id"] for v in grid.vendors if v["verdict"] == "fail"}

# --- Bid summary tiles and supplier cards -------------------------------------------------
# A vendor who replied but whose reply is not read yet is "Not read yet", never "awaiting
# reply" (UX audit H8): the cells are empty only because reading has not run.
unread = {v["id"] for v in grid.vendors if v["replied"] and all(
    grid.cells[(v["id"], ln["id"])].state == CellState.AWAITING for ln in grid.lines)}
summary_box = st.container(border=True)
summary_box.subheader("Bid summary")
summary_box.caption("Each vendor's annual value, lines quoted and open issues. Select a "
                    "vendor's name below for their supplier card.")
tiles = []
for v in grid.vendors:
    s = summary[v["id"]]
    total = copy.inr_short(s["total"]) if s["total"] is not None else "—"
    held = (f" · {copy.plural(s['held_back'], 'price')} left out until checked"
            if s["held_back"] else "")
    # Quoted lines with no price in this view (e.g. no delivered price) are not in the total.
    missing = s["quoted"] - s["held_back"] - s["priced"]
    if view == "landed" and missing > 0:
        held += f" · {missing} without a delivered price, left out"
    verdict = "Answers not read yet" if v["id"] in unread else copy.VERDICT_LABEL[v["verdict"]]
    tiles.append((v["name"], "Not read yet" if v["id"] in unread else
                   total if v["replied"] else "Awaiting reply",
                  f"{s['quoted']}/{s['lines']} lines · "
                  f"{copy.plural(tracked[v['id']]['Open issues'], 'open issue')} · "
                  f"{verdict}{held}"))
with summary_box:
    style.tiles(tiles, muted={i for i, v in enumerate(grid.vendors) if v["id"] in failed})
basis = BASIS[view] if view != "as_quoted" else "not comparable as quoted"
summary_box.caption(f"Annual value of each vendor's priced lines ({basis}; change it under "
                    "Price comparison). Lines without a price are left out, never counted as "
                    "zero; so is a price that looks like a different unit until it is checked.")

cards = summary_box.columns(len(grid.vendors))
for col, v in zip(cards, grid.vendors, strict=True):
    with col.popover(v["name"], width="stretch"):
        card = data.supplier_card(rfx["id"], v["id"])
        st.markdown(f"**{card['name']}**" + ("  \nCurrent supplier" if card["incumbent"] else ""))
        st.caption(card["email"] or "")
        if v["id"] in reasons:
            st.warning(reasons[v["id"]])
        if card["contract_lines"]:
            st.write(f"Supplied {card['contract_lines']} lines under the "
                     f"{', '.join(card['periods']) or 'previous'} contract.")
        elif card["quoted_last_year"]:
            st.write(f"Quoted {card['quoted_last_year']} lines last year; not awarded.")
        else:
            st.write("No earlier contract with us on record.")
        if card["certificates"]:
            st.dataframe(card["certificates"], hide_index=True, width="stretch")

st.subheader("Bids side by side")
st.caption("Commercial: every vendor's price for each line. Technical: questionnaire answers "
           "and the documents vendors sent.")
tab_com, tab_tech = st.tabs(["Commercial", "Technical"])


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _why(a: dict, q: dict) -> str:
    """Why an answer meets or not, in plain words (a failed requirement says why)."""
    if q["is_gating"] and a["verdict"] == "fail":
        return copy.requirement_reason(a["rationale"], q["text"]) + "."
    return copy.plain(a["rationale"] or "")


def short_names(vendors: list[dict]) -> dict[int, str]:
    """Each vendor's first word ('Kaveri'), or more words when two would look the same;
    the full name is in the tiles and the column tooltip (UX audit M4)."""
    out = {}
    for v in vendors:
        words = v["name"].split()
        n = 1
        while n < len(words) and any(o["id"] != v["id"] and o["name"].split()[:n] == words[:n]
                                     for o in vendors):
            n += 1
        out[v["id"]] = " ".join(words[:n])
    return out


SHORT = short_names(grid.vendors)


def header(v: dict) -> str:
    """Short vendor name plus the requirement symbol; the full reason is in the tooltip."""
    return f"{SHORT[v['id']]} {copy.VERDICT_SYMBOL[v['verdict']]}"


def _selection(key: str) -> dict:
    """The current selection of the st.dataframe with this key, from the last rerun."""
    state = st.session_state.get(key)
    try:
        return state["selection"] if state else {}
    except (KeyError, TypeError):
        return {}


def selected_cell(key: str) -> tuple[int, str] | None:
    """(row, column) of the selected cell, read before the grid is drawn so the page can
    decide whether to open the side panel."""
    cells = _selection(key).get("cells") or []
    return (cells[0][0], cells[0][1]) if cells else None


def selected_rows(key: str) -> list[int]:
    return list(_selection(key).get("rows") or [])


with tab_com, st.container(border=True):
    st.subheader("Price comparison")
    st.caption("Prices line by line. Select a price to see where it came from, beside the "
               "grid.")
    view_picked = st.segmented_control("Price basis", list(VIEWS), format_func=VIEWS.get,
                                       default="unit", key="view") or "unit"
    if view_picked != view:  # the tiles above were drawn with the old basis
        st.rerun()
    legend = " · ".join(f"{data.STATE_SYMBOL[s]} {data.STATE_LABEL[s]}" for s in CellState)
    st.caption(f"Prices in {BASIS[view]}. {legend}. L1, L2, L3: lowest eligible prices on the "
               "line (verified or assumed, from vendors that meet the requirements); L1 in bold.")
    cols = [header(v) for v in grid.vendors]
    rows, states, bold = [], [], []
    for line in grid.lines:
        row = {"Line": f"{line['line_no']}. {line['description']}",
               "Annual qty": f"{copy.inr(data._dec(line['annual_qty'])).lstrip('₹')} "
                             f"{line['canonical_uom']}"}
        row_states, row_bold = {}, set()
        for v, col in zip(grid.vendors, cols, strict=True):
            c = grid.cells[(v["id"], line["id"])]
            rank = ranks.get((v["id"], line["id"])) if view != "as_quoted" else None
            row[col] = (f"{data.STATE_SYMBOL[c.state]} not read yet" if v["id"] in unread
                        else data.cell_text(c, view, rank))
            row_states[col] = c.state
            if rank == 1:
                row_bold.add(col)
        rows.append(row)
        states.append(row_states)
        bold.append(row_bold)
    rows.append({"Line": "Total, priced lines", "Annual qty": ""}
                | {col: copy.inr_short(summary[v["id"]]["total"])
                   for v, col in zip(grid.vendors, cols, strict=True)})
    rows.append({"Line": "Lines quoted", "Annual qty": ""}
                | {col: f"{summary[v['id']]['quoted']}/{summary[v['id']]['lines']}"
                   for v, col in zip(grid.vendors, cols, strict=True)})
    if view != "as_quoted":  # how many lines the total covers (QA pass 3 H7)
        rows.append({"Line": "Lines in the total", "Annual qty": ""}
                    | {col: f"{summary[v['id']]['priced']}/{summary[v['id']]['lines']}"
                       for v, col in zip(grid.vendors, cols, strict=True)})
    rows.append({"Line": "Open issues", "Annual qty": ""}
                | {col: str(tracked[v["id"]]["Open issues"])
                   for v, col in zip(grid.vendors, cols, strict=True)})
    df = pd.DataFrame(rows)
    grey = {col for v, col in zip(grid.vendors, cols, strict=True) if v["id"] in failed}

    def colour(frame_: pd.DataFrame) -> pd.DataFrame:
        styles = pd.DataFrame("", index=frame_.index, columns=frame_.columns)
        for i, row_states in enumerate(states):
            for col, state in row_states.items():
                css = f"background-color: {data.STATE_COLOUR[state]}"
                if col in bold[i]:
                    css += "; font-weight: 700"
                if col in grey:
                    css += "; color: #9AA5B1"
                styles.loc[i, col] = css
        styles.iloc[len(states):, :] = "background-color: #F4F6F8; font-weight: 600"
        return styles

    # The selection is read before the grid is drawn, so the page knows whether to split.
    # Only line rows have receipts; the last rows are totals.
    # One key for every price basis: rows and columns are the same in each, so an open
    # receipt stays open when the basis changes.
    grid_key = "grid"
    raw = selected_cell(grid_key)
    picked = raw if raw and raw[0] < len(grid.lines) and raw[1] in cols else None
    picked = side_panel.keep_open(grid_key, raw, picked)  # a click on a total keeps it open
    if picked and not (picked[0] < len(grid.lines) and picked[1] in cols):
        picked = None  # remembered from another event
    left, right = side_panel.columns(picked is not None)
    with left:
        # Grid at full width when nothing is selected; beside the panel it scrolls sideways
        # inside itself with the line column pinned. Sized to fit five vendors in about
        # 870 px, the content width at a 1280 px screen (QA pass 3 H5). Text as quoted is
        # left-aligned so it is cut at its end, not start.
        st.dataframe(
            df.style.apply(colour, axis=None), hide_index=True, width="stretch",
            height="content", on_select="rerun",  # every line and the totals, no inner scroll
            selection_mode="single-cell", key=grid_key,
            column_config={"Line": st.column_config.TextColumn(width=200, pinned=True),
                           "Annual qty": st.column_config.TextColumn(
                               width=110, pinned=picked is None, alignment="right")}
            | {col: st.column_config.TextColumn(
                width=112, alignment="left" if view == "as_quoted" else "right",
                help=f"{v['name']}: {reasons.get(v['id'], copy.VERDICT_LABEL[v['verdict']])}")
               for v, col in zip(grid.vendors, cols, strict=True)})
        st.caption("Totals include assumed prices and prices still to check; '—' lines are "
                   "never priced as zero.")
        if picked is None:
            st.info("Select a price in the grid to see the vendor's own words, how it was "
                    "converted, and which checks it passed. It opens beside the grid.")
    if picked is not None:
        row_i, col = picked
        line = grid.lines[row_i]
        vendor = grid.vendors[cols.index(col)]
        r = data.receipt(grid, vendor["id"], line["id"])
        with side_panel.panel(right, "price", receipt.title(r, line, vendor), grid_key,
                              subtitle=f"{line['description']} · {BASIS[view]}"):
            receipt.show(r, line, vendor, key=f"r_{vendor['id']}_{line['id']}",
                         heading=False)

with tab_tech:
    questions, answers = data.questionnaire(rfx["id"])
    replied = [v for v in grid.vendors if v["replied"]]
    with st.container(border=True):
        st.subheader("Questionnaire")
        st.caption("Each vendor's answers to your questions. Required questions decide "
                   "whether a vendor can be awarded.")
        if questions and replied:  # the verdict first, so a "Does not meet" is in view (M6)
            st.html('<table class="lv-ctx">' + "".join(
                f'<tr><td class="k">{escape(v["name"])}</td><td>'
                f'{copy.VERDICT_SYMBOL[v["verdict"]]} {copy.VERDICT_LABEL[v["verdict"]]}</td>'
                f'<td>{escape(_cap(reasons.get(v["id"], "").removeprefix("Does not meet ")))}'
                '</td></tr>' for v in replied) + "</table>")
        if not questions:
            st.info("This event has no questionnaire. Add questions on the RFx page before "
                    "sending.")
        else:
            q_rows = []
            for q in questions:
                must = " (required)" if q["is_gating"] else ""
                row = {"Question": f"{q['q_no']}. {q['text']}{must}"}
                for v in replied:
                    a = answers.get((q["id"], v["id"]))
                    if not a:
                        row[v["name"]] = "? not answered" if q["is_gating"] else "not answered"
                    elif q["is_gating"]:  # only a required question has a pass or fail (QA M7)
                        row[v["name"]] = (f"{copy.VERDICT_SYMBOL.get(a['verdict'], '?')} "
                                          f"{a['value']}")
                    else:
                        row[v["name"]] = a["value"]
                q_rows.append(row)
            raw = selected_cell("qgrid")
            cell = raw if raw and raw[1] != "Question" and raw[0] < len(questions) and any(
                v["name"] == raw[1] for v in replied) else None
            cell = side_panel.keep_open("qgrid", raw, cell)
            if cell and (cell[0] >= len(questions) or all(v["name"] != cell[1] for v in replied)):
                cell = None  # remembered from another event
            left, right = side_panel.columns(cell is not None)
            with left:
                st.dataframe(
                    pd.DataFrame(q_rows), hide_index=True, width="stretch", height="content",
                    on_select="rerun", selection_mode="single-cell", key="qgrid",
                    column_config={"Question": st.column_config.TextColumn(
                        width=300 if cell is None else 220, pinned=True)}
                    | {v["name"]: st.column_config.TextColumn(width=180) for v in replied})
                st.caption("Required questions: ✓ meets · ✖ does not meet · ? unclear or not "
                           "answered. Other answers are shown as the vendor gave them, cut to "
                           "fit. Select an answer to read it in full with the vendor's own "
                           "words.")
            if cell is not None:
                q = questions[cell[0]]
                v = next(v for v in replied if v["name"] == cell[1])
                a = answers.get((q["id"], v["id"]))
                verdict = copy.VERDICT_LABEL.get((a or {}).get("verdict") or "unclear")
                with side_panel.panel(right, "answer",
                                      f"Question {q['q_no']} · {v['name']} · {verdict}",
                                      "qgrid", subtitle=q["text"]):
                    if a is None:
                        st.write("No answer given. Treated as unclear, never as a pass.")
                    else:
                        st.markdown(f"**Answer:** {a['value']}")
                        if why := _why(a, q):
                            st.markdown(f"**Why:** {why}")
                        source_viewer.preview(Evidence(attachment_id=a["attachment_id"],
                                                       locator=a["evidence_locator"] or "",
                                                       quote=a["evidence_quote"] or ""),
                                              key=f"qa_{a['id']}")

    with st.container(border=True):
        st.subheader("Documents")
        st.caption("Every file vendors sent: quotations, certificates and reports. Select one, "
                   "then open it full size from the panel beside the list.")
        docs = data.documents(rfx["id"])
        if not docs:
            st.info("No documents received yet. Quotations, certificates and reports appear "
                    "here as replies arrive.")
        else:
            rows_sel = selected_rows("docs")
            d = docs[rows_sel[0]] if rows_sel and rows_sel[0] < len(docs) else None
            left, right = side_panel.columns(d is not None)
            with left:
                st.dataframe(
                    pd.DataFrame([{"Vendor": x["vendor_name"], "File": x["filename"],
                                   "Kind": (x["kind"] or "").capitalize(),
                                   "Size (KB)": round((x["size_bytes"] or 0) / 1024, 1),
                                   "Received": copy.date_time(x["received_at"])}
                                  for x in docs]),
                    hide_index=True, width="stretch", on_select="rerun",
                    selection_mode="single-row", key="docs")
            if d is not None:
                with side_panel.panel(right, "document", f"{d['filename']} · {d['vendor_name']}",
                                      "docs", subtitle=(f"{(d['kind'] or 'File').capitalize()}"
                                                        f" · received "
                                                        f"{copy.date_time(d['received_at'])}")):
                    source_viewer.preview(Evidence(attachment_id=d["id"], locator="", quote=""),
                                          key=f"doc_{d['id']}")
