"""WP12: no internal term reaches a page, an export or an email (docs/UX_GUIDE.md section 2).

Each buyer page is rendered with Streamlit's AppTest against the invented mock event, and
every piece of text it shows is checked against the banned list in level/copy.py. Exports
are generated and their text checked the same way.
"""

import io
import threading

import pytest
from components import backend, data, mock_backend, mock_seed
from docx import Document
from openpyxl import load_workbook
from streamlit.testing.v1 import AppTest

from level import copy, worker
from level.agents.analyst import award, tools

PAGES = ["views/0_Overview.py", "views/1_RFx.py", "views/2_Inbox.py", "views/3_Review.py",
         "views/4_Compare.py", "views/5_Analyst.py", "views/6_Award.py"]


@pytest.fixture
def event(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("LEVEL_PASSCODE", raising=False)
    monkeypatch.setattr(backend, "rebuild_cells", mock_backend.rebuild_cells)
    # The pages only read here; a real worker thread would outlive the test and pick up
    # jobs from later tests' databases.
    monkeypatch.setattr(worker, "start", threading.current_thread)
    return mock_seed.seed()


def _texts(at: AppTest) -> list[str]:
    """Every string the page shows: labels, bodies, options, table cells and headers."""
    out = []

    def walk(node):
        proto = getattr(node, "proto", None)
        kind = getattr(node, "type", "")
        if kind in ("arrow_data_frame", "dataframe"):
            out.append(node.value.to_string())  # cells and column names, not raw bytes
        elif proto is not None and kind not in ("code",):  # SQL in the trace is verbatim
            out.extend(_strings(proto))
        for child in getattr(node, "children", {}).values():
            walk(child)

    walk(at._tree)
    return out


def _strings(msg) -> list[str]:
    """String values inside a protobuf message (not its field names), recursively."""
    found = []
    for _field, value in msg.ListFields():
        values = value if isinstance(value, (list, tuple)) or hasattr(value, "extend") \
            else [value]
        for v in values:
            if isinstance(v, str):
                found.append(v)
            elif hasattr(v, "ListFields"):
                found.extend(_strings(v))
    return found


@pytest.mark.parametrize("page", PAGES)
def test_no_internal_term_on_any_page(event, page):
    at = AppTest.from_file("../app/Home.py", default_timeout=60)
    at.run()
    at.switch_page(page).run()
    assert not at.exception, at.exception
    text = "\n".join(_texts(at))
    assert text.strip(), "page rendered nothing"
    assert copy.find_banned(text) == [], page


def test_no_internal_term_in_exports(event):
    ctx = tools.TurnContext(rfx_id=event)
    out = tools.run_scenario(ctx, {"strategy": "cheapest_per_line", "price_basis": "landed",
                                   "gating": "ignore"})
    award.set_pinned(out["scenario_id"], True)
    memo = Document(io.BytesIO(award.memo_docx(event, out["scenario_id"])))
    texts = [p.text for p in memo.paragraphs]
    texts += [c.text for t in memo.tables for row in t.rows for c in row.cells]
    for blob in (award.scenario_xlsx(event, out["scenario_id"]), award.comparison_xlsx(event)):
        wb = load_workbook(io.BytesIO(blob))
        for ws in wb:
            texts.append(ws.title)
            texts += [str(v) for row in ws.iter_rows(values_only=True) for v in row if v]
    found = copy.find_banned("\n".join(texts))
    assert found == [], found


def test_award_drafts_and_email_drafts_use_no_banned_word(event, monkeypatch):
    monkeypatch.delenv("LEVEL_ANTHROPIC_API_KEY", raising=False)  # fixed wording, no model
    for v in data.vendors(event):
        body = backend.clarify.draft_clarification(event, v["id"])
        if body:
            assert copy.find_banned(body, copy.BANNED_EMAIL) == [], v["name"]


def test_date_and_date_time_format_without_platform_flags():
    # 08:40 UTC is 2:10 PM IST; single-digit day and hour carry no leading zero.
    assert copy.date("2026-11-07T08:40:00Z") == "7 Nov 2026"
    assert copy.date("2025-02-24") == "24 Feb 2025"
    assert copy.date_time("2026-11-07T08:40:00Z") == "7 Nov 2026, 2:10 PM IST"
    assert copy.date_time("2026-11-17T18:35:00Z") == "18 Nov 2026, 12:05 AM IST"
    assert copy.date_time("2026-11-17T06:30:00Z") == "17 Nov 2026, 12:00 PM IST"
    assert copy.date(None) == "—" and copy.date_time("") == "—"
