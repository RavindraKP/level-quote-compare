"""Turn a file + evidence locator into something the source viewer can show.

Plain Python, no Streamlit, so it can be unit-tested. The viewer in source_viewer.py calls
these and only does layout.

Locators follow the formats in level/types.py SourceRef: 'Sheet2!D14', 'page 2',
'paragraph 7', 'table 1, row 3', 'email, line 2', 'image'.
"""

from __future__ import annotations

import html
import io
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pymupdf as fitz  # PyMuPDF
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter
from PIL import Image, ImageOps


@dataclass
class Locator:
    kind: str  # cell / page / paragraph / table / line / image / unknown
    sheet: str | None = None
    cell: str | None = None
    number: int | None = None  # page, paragraph, table or line number (1-based)
    row: int | None = None  # table row (1-based)


def parse_locator(text: str | None) -> Locator:
    """Read a locator string into its parts. Unknown formats are kept as 'unknown'."""
    t = (text or "").strip()
    if m := re.fullmatch(r"'?(.+?)'?!\$?([A-Z]{1,3})\$?(\d+)", t):
        return Locator("cell", sheet=m[1], cell=f"{m[2]}{m[3]}")
    if m := re.search(r"table\s*(\d+)\s*,\s*row\s*(\d+)", t, re.I):
        return Locator("table", number=int(m[1]), row=int(m[2]))
    if m := re.search(r"page\s*(\d+)", t, re.I):
        return Locator("page", number=int(m[1]))
    if m := re.search(r"paragraph\s*(\d+)", t, re.I):
        return Locator("paragraph", number=int(m[1]))
    if m := re.search(r"line\s*(\d+)", t, re.I):
        return Locator("line", number=int(m[1]))
    if t.lower().startswith("image"):
        return Locator("image")
    return Locator("unknown")


def file_kind(filename: str, mime: str | None = None) -> str:
    """pdf / xlsx / docx / image / text, from extension or mime."""
    name = filename.lower()
    mime = (mime or "").lower()
    if name.endswith(".pdf") or "pdf" in mime:
        return "pdf"
    if name.endswith((".xlsx", ".xlsm")) or "spreadsheet" in mime:
        return "xlsx"
    if name.endswith(".docx") or "wordprocessing" in mime:
        return "docx"
    if name.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".heic", ".tif", ".tiff")
    ) or mime.startswith("image/"):
        return "image"
    return "text"


# --- PDF ---------------------------------------------------------------------


def _search_terms(quote: str) -> list[str]:
    """What to search for, most specific first: the whole quote, its first part, then its
    longer numbers. A short number such as "1" would match all over the page, so numbers of
    fewer than three characters are never searched for (UX audit M10)."""
    quote = " ".join(quote.split())
    terms = [quote]
    if len(quote) > 40:
        terms.append(quote[:40])
    terms += [n for n in re.findall(r"\d[\d,]*\.?\d*", quote) if len(n) >= 3]
    return [t for t in terms if t]


def pdf_page_count(path: str | Path) -> int:
    with fitz.open(path) as doc:
        return doc.page_count


def pdf_page_png(path: str | Path, page_no: int, quote: str | None) -> tuple[bytes, bool]:
    """Render one page (1-based) as PNG with a red box round the quote. Returns (png, found)."""
    with fitz.open(path) as doc:
        page = doc[max(0, min(page_no, doc.page_count) - 1)]
        found = False
        terms = _search_terms(quote or "")
        n_text = 2 if len(" ".join((quote or "").split())) > 40 else 1  # quote, its first part
        for i, term in enumerate(terms):
            rects = page.search_for(term)
            if rects:
                if i >= n_text:  # a number on its own: mark its first place only
                    rects = rects[:1]
                for r in rects:
                    page.draw_rect(r + (-2, -2, 2, 2), color=(0.85, 0.1, 0.1), width=1.5)
                found = True
                break
        png = page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")  # 2x for legibility
    return png, found


def pdf_find_page(path: str | Path, quote: str) -> int | None:
    """First page (1-based) whose text contains the quote, for locators without a page."""
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            if any(page.search_for(t) for t in _search_terms(quote)[:2]):
                return i + 1
    return None


# --- Excel -------------------------------------------------------------------


def _workbook(path: str | Path, **kwargs):
    """Open a workbook from its bytes. Stored uploads have no file extension, and openpyxl
    refuses a path without .xlsx, so the bytes are passed instead (UX audit H4)."""
    return load_workbook(io.BytesIO(Path(path).read_bytes()), **kwargs)


def xlsx_sheet_names(path: str | Path) -> list[str]:
    wb = _workbook(path, read_only=True)
    names = wb.sheetnames
    wb.close()
    return names


def xlsx_sheet(path: str | Path, sheet: str | None) -> tuple[str, pd.DataFrame]:
    """A sheet as a dataframe with Excel-style column letters and row numbers."""
    wb = _workbook(path, data_only=True)
    ws = wb[sheet] if sheet in wb.sheetnames else wb.worksheets[0]
    rows = [["" if v is None else str(v) for v in row] for row in ws.iter_rows(values_only=True)]
    width = max((len(r) for r in rows), default=0)
    cols = [get_column_letter(i + 1) for i in range(width)]
    df = pd.DataFrame(rows, columns=cols, index=range(1, len(rows) + 1))
    return ws.title, df


def cell_position(cell: str) -> tuple[int, str]:
    """'D14' -> (14, 'D')."""
    m = re.fullmatch(r"([A-Z]{1,3})(\d+)", cell)
    if not m:
        raise ValueError(f"not a cell address: {cell}")
    column_index_from_string(m[1])  # raises on nonsense
    return int(m[2]), m[1]


MAX_WINDOW_COLS = 8  # columns shown in the focused window before it starts further right


def cell_window(row: int, col: str, n_rows: int, n_cols: int, rows_around: int = 5,
                cols_after: int = 2, row_first_col: int | None = None
                ) -> tuple[int, int, int, int] | None:
    """The part of a sheet to show around one cell, so the cell is always in view.

    row, col: the target ('F21' -> 21, 'F'). n_rows, n_cols: the sheet's size.
    row_first_col: the first non-empty column of the target row (1-based), used as the left
    edge only when starting at column A would make the window wider than MAX_WINDOW_COLS.
    Returns (first_row, last_row, first_col, last_col), 1-based and inclusive, or None when
    the cell is outside the sheet."""
    c = column_index_from_string(col)
    if not (1 <= row <= n_rows and 1 <= c <= n_cols):
        return None
    first_row, last_row = max(1, row - rows_around), min(n_rows, row + rows_around)
    last_col = min(n_cols, c + cols_after)
    first_col = 1
    if last_col > MAX_WINDOW_COLS:
        widest = last_col - MAX_WINDOW_COLS + 1  # as far left as 8 columns allow
        start = row_first_col if row_first_col and row_first_col <= c else widest
        first_col = min(max(start, widest, 1), c)
    return first_row, last_row, first_col, last_col


def _is_number(text: str) -> bool:
    try:
        float(text.replace(",", "").replace("₹", "").strip())
        return True
    except ValueError:
        return False


def header_row(df: pd.DataFrame, before: int) -> int | None:
    """The nearest row above `before` that looks like a table header: two or more filled
    cells, none of them a number. Falls back to row 1 when it is above `before`."""
    for r in range(before - 1, 0, -1):
        if r not in df.index:
            continue
        filled = [str(v).strip() for v in df.loc[r] if str(v).strip()]
        if len(filled) >= 2 and not any(_is_number(v) for v in filled):
            return r
    return 1 if before > 1 and 1 in df.index else None


def first_filled_col(df: pd.DataFrame, row: int) -> int | None:
    """1-based index of the first non-empty cell in a sheet row, or None."""
    if row not in df.index:
        return None
    for i, v in enumerate(df.loc[row], start=1):
        if str(v).strip():
            return i
    return None


# --- Word and plain text -----------------------------------------------------


def docx_blocks(path: str | Path) -> list[str]:
    """Paragraphs then table rows, labelled the way extraction locators name them."""
    import docx  # python-docx; imported here because it is slow to load

    document = docx.Document(str(path))
    blocks = [
        f"paragraph {i}: {p.text}"
        for i, p in enumerate((p for p in document.paragraphs if p.text.strip()), start=1)
    ]
    for t_no, table in enumerate(document.tables, start=1):
        for r_no, row in enumerate(table.rows, start=1):
            cells = " | ".join(c.text for c in row.cells)
            blocks.append(f"table {t_no}, row {r_no}: {cells}")
    return blocks


def mark_snippet(text: str, quote: str | None) -> tuple[str, bool]:
    """HTML-escape text and wrap the quote (whitespace-insensitive) in <mark>."""
    safe = html.escape(text)
    if not quote:
        return safe, False
    words = [re.escape(html.escape(w)) for w in quote.split()]
    pattern = re.compile(r"\s+".join(words), re.I) if words else None
    if pattern and pattern.search(safe):
        return pattern.sub(lambda m: f"<mark>{m[0]}</mark>", safe, count=1), True
    return safe, False


def numbered_lines(text: str) -> str:
    """Prefix each line with its number, matching 'email, line N' locators."""
    return "\n".join(f"{i:>3}  {line}" for i, line in enumerate(text.splitlines(), start=1))


# --- Images ------------------------------------------------------------------


def image_upright(path: str | Path) -> Image.Image:
    """Open an image and apply its EXIF rotation, so a sideways phone photo shows upright."""
    img = Image.open(path)
    return ImageOps.exif_transpose(img).convert("RGB")


def image_zoom(img: Image.Image, zoom: float, cx: float, cy: float) -> bytes:
    """Crop around (cx, cy) in 0..1 coordinates at the given zoom, return PNG bytes."""
    zoom = max(1.0, zoom)
    w, h = img.size
    cw, ch = w / zoom, h / zoom
    left = min(max(0, cx * w - cw / 2), w - cw)
    top = min(max(0, cy * h - ch / 2), h - ch)
    crop = img.crop((int(left), int(top), int(left + cw), int(top + ch)))
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()
