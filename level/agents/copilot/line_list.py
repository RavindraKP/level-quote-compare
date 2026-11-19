"""A buyer's own line list (pasted text, CSV or Excel) -> draft lines, in code.

The buyer's list is read as a table: a header row names the columns if it has one ("Item",
"Unit", "Qty"...), otherwise the columns are taken as description, unit, quantity. Nothing
here assumes a category; a column the reader does not know becomes a specification
attribute. The lines are saved through `draft.upsert_lines`, the same path as the co-pilot.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation

from level.pipeline import intake

MAX_LINES = 200

# Header words -> the line field they fill. Checked as whole words, first match wins.
HEADERS = [
    ("line_no", ("line no", "line", "sr no", "s no", "sl no", "no", "#", "item no")),
    ("canonical_uom", ("unit of measure", "uom", "unit", "units")),
    ("annual_qty", ("annual quantity", "annual qty", "quantity", "qty", "volume", "annual")),
    ("plant", ("plant", "location", "site", "deliver to", "delivery location")),
    ("spec", ("specification", "specifications", "spec", "specs", "details")),
    ("description", ("description", "item", "item description", "product", "material",
                     "name", "part")),
]
DEFAULT_ORDER = ["description", "canonical_uom", "annual_qty"]
# Price columns are left out: line specifications go to vendors, a reference price must not.
PRICE_WORDS = ("price", "rate", "cost", "amount", "value", "budget", "total")
SKIP = "skip"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9#]+", " ", str(text).lower()).strip()


def _field_for(header: str) -> str | None:
    h = _norm(header)
    if any(re.search(rf"(^| ){w}( |$)", h) for w in PRICE_WORDS):
        return SKIP
    for field, words in HEADERS:
        if h in words:
            return field
    for field, words in HEADERS:  # "Annual qty (pcs)", "Item description"
        if any(re.search(rf"(^| ){re.escape(w)}( |$)", h) for w in words if len(w) > 2):
            return field
    return None


def _number(text: str) -> str | None:
    """'1,200' -> '1200'; None if it is not a plain number."""
    cleaned = str(text).replace(",", "").strip()
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return str(value) if value >= 0 else None


def rows_from_text(text: str) -> list[list[str]]:
    """Pasted text: tab-separated (copied from Excel), else '|', ';' or comma-separated."""
    raw = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    if not raw:
        return []
    for sep in ("\t", "|", ";"):
        if sum(sep in ln for ln in raw) >= max(1, len(raw) // 2):
            return [[c.strip() for c in ln.split(sep)] for ln in raw]
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO("\n".join(raw)))]


def rows_from_file(data: bytes, filename: str) -> list[list[str]]:
    """The first sheet of an Excel file, or a CSV / text file, as rows of cell text.

    The type comes from the bytes (intake.detect_kind). Raises intake.Unreadable for anything
    that is not a simple table, with a reason the buyer can act on.
    """
    kind = intake.detect_kind(data, filename)
    if kind == "xlsx":
        from openpyxl import load_workbook

        try:
            ws = load_workbook(io.BytesIO(data), data_only=True, read_only=True).worksheets[0]
        except Exception as exc:
            raise intake.Unreadable("could not open this Excel file") from exc
        return [[_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
    if kind == "xls":
        import xlrd

        sheet = xlrd.open_workbook(file_contents=data).sheet_by_index(0)
        return [[_cell(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
                for r in range(sheet.nrows)]
    if kind in ("csv", "txt"):
        return rows_from_text(data.decode("utf-8-sig", errors="replace"))
    if kind == "empty":
        raise intake.Unreadable("this file is empty")
    raise intake.Unreadable("a line list must be an Excel, CSV or text file. For a PDF or Word "
                            "list, copy the table and paste it, or describe the lines to the "
                            "co-pilot")


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse(rows: list[list[str]]) -> tuple[list[dict], list[str]]:
    """Rows of cell text -> (lines for draft.upsert_lines, points the buyer should know).

    Lines get line numbers 1, 2, 3... unless the list numbers them itself.
    """
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return [], ["No lines found. Paste one line per row: description, unit, quantity."]
    header = [_field_for(c) for c in rows[0]]
    if "description" in header or sum(h not in (None, SKIP) for h in header) >= 2:
        fields, body, extra_names = header, rows[1:], rows[0]
    else:
        fields = list(DEFAULT_ORDER)
        if len(rows[0]) > 3 and all(_number(r[0]) and "." not in r[0] for r in rows):
            fields = ["line_no", *DEFAULT_ORDER]  # a numbered list without a header
        body, extra_names = rows, []
    if "description" not in fields:
        return [], ["No description column found. Name one column 'Description' or 'Item'."]
    lines, notes = [], []
    if SKIP in fields:
        notes.append("Price columns were left out: vendors see the line details, so prices "
                     "are not copied into them.")
    for n, row in enumerate(body[:MAX_LINES], start=1):
        line: dict = {"spec": {}}
        for i, value in enumerate(row):
            value = str(value).strip()
            field = fields[i] if i < len(fields) else None
            if not value or field == SKIP:
                continue
            if field == "spec":  # kept under the buyer's own column name
                name = _norm(extra_names[i]) if i < len(extra_names) else ""
                line["spec"][name.replace(" ", "_") or "details"] = value
            elif field is None:  # a column we do not know: kept as an attribute
                name = _norm(extra_names[i]) if i < len(extra_names) else f"column {i + 1}"
                line["spec"][name.replace(" ", "_") or f"column_{i + 1}"] = value
            elif field in line:  # two columns mapped to one field: keep the second as spec
                line["spec"][_norm(extra_names[i]).replace(" ", "_") or f"column_{i + 1}"] = value
            else:
                line[field] = value
        if not line.get("description"):
            notes.append(f"Row {n} has no description and was left out.")
            continue
        if "line_no" in line:
            number = _number(line["line_no"])
            line["line_no"] = int(Decimal(number)) if number and "." not in number else None
        if "annual_qty" in line:
            qty = _number(line["annual_qty"])
            if qty is None:
                notes.append(f"{line['description']}: quantity \"{line['annual_qty']}\" is not "
                             "a number; enter it in the table.")
            line["annual_qty"] = qty
        lines.append(line)
    if len(body) > MAX_LINES:
        notes.append(f"Only the first {MAX_LINES} lines were taken.")
    used = [ln["line_no"] for ln in lines if ln.get("line_no")]
    if len(set(used)) != len(used) or len(used) != len(lines):  # missing or repeated numbers
        for i, ln in enumerate(lines, start=1):
            ln["line_no"] = i
    for ln in lines:
        if not ln.get("canonical_uom"):
            notes.append(f"Line {ln['line_no']} ({ln['description']}) has no unit; add one in "
                         "the table.")
    return lines, notes
