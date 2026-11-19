"""WP4: the screens' data layer and source rendering, against the mock event (no browser)."""

from decimal import Decimal

import pytest
import sqlalchemy as sa
from components import backend, data, mock_backend, mock_seed
from components import source_render as sr

from level import db
from level.types import Cell, CellState


@pytest.fixture
def event(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    # The mock event has cells but no claims; the real rebuild would (rightly) empty them.
    monkeypatch.setattr(backend, "rebuild_cells", mock_backend.rebuild_cells)
    return mock_seed.seed()


def _cell(grid, vendor_name, line_no):
    vendor = next(v for v in grid.vendors if v["name"] == vendor_name)
    line = next(line for line in grid.lines if line["line_no"] == line_no)
    return grid.cells[(vendor["id"], line["id"])]


def test_every_vendor_line_has_exactly_one_state(event):
    grid = data.load_grid(event)
    assert len(grid.cells) == len(grid.lines) * len(grid.vendors)
    awaiting = [v for v in grid.vendors if not v["replied"]]
    assert awaiting and all(
        grid.cells[(v["id"], line["id"])].state == CellState.AWAITING
        for v in awaiting
        for line in grid.lines
    )


def test_not_quoted_never_looks_like_a_price(event):
    grid = data.load_grid(event)
    c = _cell(grid, "Orbit Industrial Supplies", 8)
    assert c.state == CellState.NOT_QUOTED
    for view in data.VIEWS:
        text = data.cell_text(c, view)
        assert "not quoted" in text and not any(ch.isdigit() for ch in text)
        assert data.cell_value(c, view) is None


def test_lowest_skips_failed_gating_and_unreviewed(event):
    grid = data.load_grid(event)
    lowest = data.lowest_by_line(grid, "unit")
    by_no = {line["line_no"]: line["id"] for line in grid.lines}
    names = {v["id"]: v["name"] for v in grid.vendors}
    # Harbor is cheapest on line 1 but fails a gating question, so it cannot be marked lowest.
    assert names[lowest[by_no[1]]] != "Harbor PPE Traders"
    # Orbit's line 4 needs review, so it is not eligible either.
    assert names[lowest[by_no[4]]] == "Northwind Safety Co"


def test_summary_counts_coverage(event):
    grid = data.load_grid(event)
    summary = data.vendor_summary(grid, "unit")
    orbit = next(v for v in grid.vendors if v["name"] == "Orbit Industrial Supplies")
    assert (summary[orbit["id"]]["quoted"], summary[orbit["id"]]["lines"]) == (7, 8)
    assert data.vendor_summary(grid, "as_quoted")[orbit["id"]]["total"] is None


def test_resolving_a_flag_logs_it_and_updates_the_cell(event):
    flag = next(f for f in data.open_flags(event) if f.type.value == "read_mismatch")
    data.resolve_flag(flag.id, "edit_value", "345", note="read it myself")
    c = _cell(data.load_grid(event), "Harbor PPE Traders", 6)
    assert c.unit_price_inr == Decimal("345")
    assert c.state == CellState.CONFIRMED and c.confirmed_by.value == "buyer"
    with db.connect() as conn:
        audit = conn.execute(sa.select(db.audit_event)).mappings().all()
    assert [a["action"] for a in audit] == ["flag.edit_value"]
    assert flag.id not in [f.id for f in data.open_flags(event)]


def test_edit_value_rejects_non_numbers(event):
    flag = next(f for f in data.open_flags(event) if f.type.value == "read_mismatch")
    with pytest.raises(ValueError):
        data.resolve_flag(flag.id, "edit_value", "about 300")


def test_mark_not_quoted(event):
    flag = next(f for f in data.open_flags(event) if f.type.value == "spec_deviation")
    data.resolve_flag(flag.id, "not_quoted")
    c = _cell(data.load_grid(event), "Harbor PPE Traders", 7)
    assert c.state == CellState.NOT_QUOTED and c.unit_price_inr is None


def test_changing_fx_recomputes_usd_cells(event):
    before = _cell(data.load_grid(event), "Orbit Industrial Supplies", 1).unit_price_inr
    data.set_assumption("fx.USDINR", "90", "test", "2026-11-17", event)
    after = _cell(data.load_grid(event), "Orbit Industrial Supplies", 1).unit_price_inr
    assert before == Decimal("9.2925") and after == Decimal("9.4500")


def test_add_reply_twice_is_one_reply_and_one_job(event):
    grid = data.load_grid(event)
    summit = next(v for v in grid.vendors if v["name"] == "Summit Workwear")
    files = [data.Upload("quote.txt", b"Gloves Rs 9 per pair")]
    first = data.add_reply(event, summit["id"], None, files, "see attached")
    second = data.add_reply(event, summit["id"], None, files, "see attached")
    assert first == second
    assert len(data.messages(event, "in")) == 4
    # Replied but not read yet: awaiting, never "not quoted".
    assert _cell(data.load_grid(event), "Summit Workwear", 1).state == CellState.AWAITING


def test_add_reply_needs_content(event):
    with pytest.raises(ValueError):
        data.add_reply(event, None, "New Co", [], "  ")


def test_receipt_has_evidence_steps_and_checks(event):
    grid = data.load_grid(event)
    c = _cell(grid, "Orbit Industrial Supplies", 1)
    r = data.receipt(grid, c.vendor_id, c.rfx_line_id)
    assert r.evidence.locator == "page 1" and "0.105" in r.evidence.quote
    assert r.conversion_steps[0].source == "fx.USDINR"
    assert {ch.name for ch in r.checks} >= {"Found in the vendor's document",
                                            "In line with other quotes"}


def test_format_inr():
    assert data.format_inr(Decimal("7538000")) == "₹75,38,000"
    assert data.format_inr(Decimal("999")) == "₹999"
    assert data.format_inr_short(Decimal("30530730")) == "₹3.05 Cr"
    assert data.format_inr(None) == "—"


def test_cell_text_marks_state_with_symbol():
    c = Cell(
        rfx_id=1,
        vendor_id=1,
        rfx_line_id=1,
        state=CellState.NEEDS_REVIEW,
        reason="x",
        unit_price_inr=Decimal("12.5"),
    )
    assert data.cell_text(c, "unit", rank=1) == "⚠ 12.50  L1"
    assert data.cell_text(c, "unit") == "⚠ 12.50"


def test_ranks_tag_only_eligible_prices_l1_to_l3(event):
    grid = data.load_grid(event)
    ranks = data.ranks_by_line(grid, "unit")
    failed = {v["id"] for v in grid.vendors if v["verdict"] == "fail"}
    for (vendor_id, line_id), rank in ranks.items():
        c = grid.cells[(vendor_id, line_id)]
        assert 1 <= rank <= 3 and vendor_id not in failed
        assert c.state in data.ELIGIBLE_STATES
    for line_id, vendor_id in data.lowest_by_line(grid, "unit").items():
        assert ranks[(vendor_id, line_id)] == 1


@pytest.mark.parametrize(
    "text,kind,expected",
    [
        ("Sheet2!D14", "cell", ("Sheet2", "D14")),
        ("page 2", "page", 2),
        ("paragraph 7", "paragraph", 7),
        ("table 1, row 3", "table", (1, 3)),
        ("email, line 2", "line", 2),
        ("image", "image", None),
    ],
)
def test_parse_locator(text, kind, expected):
    loc = sr.parse_locator(text)
    assert loc.kind == kind
    if kind == "cell":
        assert (loc.sheet, loc.cell) == expected
    elif kind == "table":
        assert (loc.number, loc.row) == expected
    elif expected is not None:
        assert loc.number == expected


def test_mark_snippet_escapes_and_marks():
    html, found = sr.mark_snippet("Price <b>: Rs 9.40  per pair.", "Rs 9.40 per pair.")
    assert found and "<mark>Rs 9.40  per pair.</mark>" in html and "&lt;b&gt;" in html


def test_pdf_highlight_finds_quote(event):
    att = next(d for d in data.documents(event) if d["filename"].endswith(".pdf"))
    png, found = sr.pdf_page_png(att["path"], 1, "Freight extra at actuals.")
    assert found and png[:4] == b"\x89PNG"
