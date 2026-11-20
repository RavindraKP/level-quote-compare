"""Show a source document with the evidence highlighted. Layout only; see source_render.py."""

from __future__ import annotations

import logging
from html import escape
from pathlib import Path

import streamlit as st

from components import data
from components import source_render as sr
from level import copy
from level.types import Evidence

log = logging.getLogger(__name__)


def quote(text: str) -> None:
    """The vendor's own words, in the same quote style as the Review card (UX audit B9)."""
    st.html(f'<div class="lv-dim">Vendor wrote:</div><div class="lv-quote">“{escape(text)}”'
            "</div>")


def preview(evidence: Evidence | None, key: str, show_quote: bool = True,
            email_body: str | None = None) -> None:
    """The source in a narrow side panel: where the value is, the cited spreadsheet cell
    with its neighbours, the vendor's words, and a button that opens the whole file in a
    large window (sheet picker, pages and zoom work there without moving the panel)."""
    if evidence is None or (att := data.attachment(evidence.attachment_id)) is None:
        show(evidence, key, show_quote=show_quote, email_body=email_body)  # no file: compact
        return
    path = Path(att["path"])
    st.caption(f"**{att['filename']}** · {copy.plain(evidence.locator) or 'whole file'}")
    if not path.exists():
        st.error("The original file is no longer on this server. Ask the vendor to send it "
                 "again, or add it with Add a reply.")
        return
    loc = sr.parse_locator(evidence.locator)
    if sr.file_kind(att["filename"], att["mime"]) == "xlsx" and loc.kind == "cell":
        try:
            _cell_window(path, loc)
        except Exception:  # a broken file must not break the panel; the log keeps why
            log.exception("could not show %s", att["filename"])
    if evidence.quote and show_quote:
        quote(evidence.quote)
    if st.button("Open full size", key=f"{key}_open", width="stretch",
                 icon=":material/open_in_full:", help=f"Open {att['filename']} in a large window"):
        st.dialog(att["filename"], width="large")(show)(evidence, f"{key}_full",
                                                        show_quote, email_body)


def show(evidence: Evidence | None, key: str, show_quote: bool = True,
         email_body: str | None = None) -> None:
    """Render the file behind a piece of evidence, with the quoted words marked.

    show_quote=False leaves out the "Vendor wrote" box under the file, for pages that
    already show the vendor's words above it (Review). email_body: the email text, for a
    reply written in the email itself (no file)."""
    if evidence is None:
        st.info("There is no document behind this value; it came from our own records or "
                "a decision.")
        return
    att = data.attachment(evidence.attachment_id)
    if att is None:  # a reply in the email body, or on the reply link: no file (UX R8)
        where = copy.plain(evidence.locator) if evidence.locator else ""
        if where:
            st.caption(where)
        if evidence.quote and show_quote:
            quote(evidence.quote)
        if email_body:
            with st.expander("Show their whole email"):
                show_message_body(email_body, evidence.quote)
        return
    path = Path(att["path"])
    st.caption(f"**{att['filename']}** · {copy.plain(evidence.locator) or 'whole file'}")
    if not path.exists():
        st.error("The original file is no longer on this server. Ask the vendor to send it "
                 "again, or add it with Add a reply.")
        return
    loc = sr.parse_locator(evidence.locator)
    kind = sr.file_kind(att["filename"], att["mime"])
    try:
        if kind == "pdf":
            _pdf(path, loc, evidence.quote, key)
        elif kind == "xlsx":
            _xlsx(path, loc, key)
        elif kind == "docx":
            _text_blocks(sr.docx_blocks(path), evidence.quote, loc)
        elif kind == "image":
            _image(path, key)
        else:
            _plain(path.read_text(encoding="utf-8", errors="replace"), evidence.quote)
    except Exception:  # a broken file must not break the page; the log keeps why
        log.exception("could not show %s", att["filename"])
        st.error("This file cannot be shown here. The original is kept unchanged; ask the "
                 "vendor for a PDF or Excel copy if you need to read it.")
    if evidence.quote and show_quote:
        quote(evidence.quote)


def show_message_body(body: str, quote: str | None) -> None:
    _plain(body, quote)


def _pdf(path: Path, loc: sr.Locator, quote: str, key: str) -> None:
    pages = sr.pdf_page_count(path)
    default = loc.number or (sr.pdf_find_page(path, quote) if quote else None) or 1
    page = (
        st.number_input("Page", 1, pages, min(default, pages), key=f"{key}_page")
        if pages > 1
        else 1
    )
    png, found = sr.pdf_page_png(path, int(page), quote)
    if quote and not found:
        st.warning("The vendor's words were not found on this page. Try another page.")
    st.image(png, width="stretch")


def _xlsx(path: Path, loc: sr.Locator, key: str) -> None:
    sheets = sr.xlsx_sheet_names(path)
    index = sheets.index(loc.sheet) if loc.sheet in sheets else 0
    sheet = (
        st.selectbox("Sheet", sheets, index=index, key=f"{key}_sheet")
        if len(sheets) > 1
        else sheets[0]
    )
    title, df = sr.xlsx_sheet(path, sheet)
    target = sr.cell_position(loc.cell) if loc.kind == "cell" and loc.sheet == title else None
    if not _window_grid(df, loc, target):  # no cell to point at, or not on this sheet
        _sheet_grid(df, target, height=320)
        return
    with st.expander("Show the whole sheet"):
        _sheet_grid(df, target, height=320)


def _cell_window(path: Path, loc: sr.Locator) -> None:
    """Only the part of the cited sheet around the cell (for the side panel)."""
    title, df = sr.xlsx_sheet(path, loc.sheet)
    if title == loc.sheet:
        _window_grid(df, loc, sr.cell_position(loc.cell), rows_around=3)


def _window_grid(df, loc: sr.Locator, target: tuple[int, str] | None,
                 rows_around: int = 5) -> bool:
    """The rows and columns around the cited cell, headings on top. False if no window."""
    window = None
    if target:
        window = sr.cell_window(target[0], target[1], len(df.index), len(df.columns),
                                rows_around=rows_around,
                                row_first_col=sr.first_filled_col(df, target[0]))
    if window is None:
        return False
    first_row, last_row, first_col, last_col = window
    head = sr.header_row(df, target[0])  # the headings of the table the cell is in
    above = [head] if head and head < first_row else []  # pinned on top when out of range
    rows = above + list(range(first_row, last_row + 1))
    part = df.loc[rows, df.columns[first_col - 1:last_col]]
    st.caption(f"The vendor's figure: sheet {loc.sheet}, cell {loc.cell}"
               + (f". Row {head} shows the table's headings." if head else "."))
    _sheet_grid(part, target, header=head, height="content", pin_target=True)
    return True


def _sheet_grid(df, target: tuple[int, str] | None, header: int | None = None,
                height="content", pin_target: bool = False) -> None:
    """A sheet (or part of one) with Excel row numbers and column letters, the cited cell
    marked, a heading row greyed, and long text columns capped so prices stay in view.
    pin_target keeps the cited column next to the row numbers, so it stays in view even
    in a narrow panel (its letter still says where it is in the sheet)."""
    config = {c: st.column_config.TextColumn(width=300) for c in df.columns
              if df[c].astype(str).str.len().max() > 40}
    if pin_target and target and target[1] in df.columns:
        config[target[1]] = st.column_config.TextColumn(pinned=True)
    # Row numbers as text so a heading row above the window keeps its real number.
    shown = df.set_axis([str(r) for r in df.index])
    target_label = (str(target[0]), target[1]) if target else None
    header_label = str(header) if header else None

    def marks_text(frame):
        styles = frame.astype(str).map(lambda _: "")
        if header_label in styles.index:
            styles.loc[header_label, :] = "background-color: #F1F3F5; font-weight: 600"
        if target_label and target_label[0] in styles.index and \
                target_label[1] in styles.columns:
            styles.loc[target_label] = "background-color: #FFC233; font-weight: 700"
        return styles

    st.dataframe(shown.style.apply(marks_text, axis=None), width="stretch", height=height,
                 column_config=config)


def _text_blocks(blocks: list[str], quote: str, loc: sr.Locator) -> None:
    html_parts, found = [], False
    for block in blocks:
        marked, hit = sr.mark_snippet(block, quote if not found else None)
        found = found or hit
        html_parts.append(f"<div style='margin:2px 0'>{marked}</div>")
    if quote and not found:
        st.warning("The vendor's words were not found in this document.")
    _scroll_box("".join(html_parts))


def _plain(text: str, quote: str | None) -> None:
    marked, found = sr.mark_snippet(sr.numbered_lines(text), quote)
    if quote and not found:
        st.warning("The vendor's words were not found in this text.")
    _scroll_box(f"<pre style='white-space:pre-wrap;margin:0'>{marked}</pre>")


def _image(path: Path, key: str) -> None:
    img = sr.image_upright(path)
    zoom = st.slider("Zoom", 1.0, 4.0, 1.0, 0.5, key=f"{key}_zoom")
    if zoom > 1:
        c1, c2 = st.columns(2)
        cx = c1.slider("Left ↔ right", 0.0, 1.0, 0.5, 0.05, key=f"{key}_cx")
        cy = c2.slider("Top ↕ bottom", 0.0, 1.0, 0.5, 0.05, key=f"{key}_cy")
        st.image(sr.image_zoom(img, zoom, cx, cy), width="stretch")
    else:
        st.image(img, width="stretch")
    st.caption("This is a photo, so the vendor's words cannot be highlighted automatically.")


def _scroll_box(inner_html: str) -> None:
    st.html(
        "<div style='max-height:320px;overflow-y:auto;border:1px solid #ddd;border-radius:6px;"
        f"padding:8px;font-size:0.85rem;background:#fafafa'>{inner_html}</div>"
    )
