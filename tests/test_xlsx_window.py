"""The Excel source viewer's focused window: the cited cell is always inside it."""

import pandas as pd
from components import source_render as sr


def test_window_around_a_cell_in_the_middle():
    # F21 in a 60-row, 10-column sheet: rows 16-26, columns A to H (F + 2).
    assert sr.cell_window(21, "F", 60, 10) == (16, 26, 1, 8)


def test_window_is_clamped_at_the_sheet_edges():
    assert sr.cell_window(2, "B", 4, 3) == (1, 4, 1, 3)
    assert sr.cell_window(60, "J", 60, 10) == (55, 60, 3, 10)  # at most 8 columns


def test_window_starts_at_the_rows_first_text_column_when_far_right():
    # Target in column N with text starting in column K: the window starts at K, not A.
    assert sr.cell_window(5, "N", 10, 20, row_first_col=11) == (1, 10, 11, 16)
    # A first text column right of the target is ignored.
    first_row, last_row, first_col, last_col = sr.cell_window(5, "N", 10, 20,
                                                              row_first_col=16)
    assert first_col <= 14 <= last_col


def test_cell_outside_the_sheet_has_no_window():
    assert sr.cell_window(99, "B", 10, 5) is None
    assert sr.cell_window(3, "Z", 10, 5) is None


def test_target_always_inside_the_window():
    for row in range(1, 31):
        for col in "ABCDEFGHIJKLMNO":
            w = sr.cell_window(row, col, 30, 15, row_first_col=3)
            c = ord(col) - 64
            assert w[0] <= row <= w[1] and w[2] <= c <= w[3]
            assert w[3] - w[2] + 1 <= sr.MAX_WINDOW_COLS


def _sheet(rows):
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    return pd.DataFrame(rows, columns=list("ABCDEFGH"[:width]), index=range(1, len(rows) + 1))


def test_header_row_is_the_nearest_text_row_above():
    df = _sheet([["Price list"], [""], ["Code", "Item", "Rate"], ["A1", "Glove", "9.50"],
                 ["A2", "Goggle", "139"], ["A3", "Plug", "6.10"]])
    assert sr.header_row(df, 4) == 3
    assert sr.header_row(df, 6) == 3  # number rows are skipped


def test_header_row_falls_back_to_row_one():
    df = _sheet([["1", "2"], ["3", "4"], ["5", "6"]])
    assert sr.header_row(df, 3) == 1
    assert sr.header_row(df, 1) is None


def test_first_filled_col():
    df = _sheet([["", "", "x", "y"]])
    assert sr.first_filled_col(df, 1) == 3
    assert sr.first_filled_col(df, 9) is None
