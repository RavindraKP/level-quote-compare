"""Try it with your own files: the sandbox event, its caps, line lists and delete (no model)."""

import hashlib
import json

import pytest
import sqlalchemy as sa
from components import backend, data, mock_backend, mock_seed

from level import db, demo, sandbox
from level.agents.copilot import draft, line_list
from level.pipeline import intake, run


@pytest.fixture
def demo_event(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(backend, "rebuild_cells", mock_backend.rebuild_cells)
    return mock_seed.seed()


def _dump() -> str:
    """Every row of every table, plus every stored file's bytes: the whole saved state."""
    out = {}
    with db.connect() as conn:
        for name, table in sorted(db.metadata.tables.items()):
            rows = [dict(r) for r in conn.execute(sa.select(table)).mappings()]
            out[name] = sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)
    files = {}
    for p in sorted(db.upload_dir().rglob("*")):
        if p.is_file():
            files[str(p.relative_to(db.upload_dir()))] = hashlib.sha256(p.read_bytes()).hexdigest()
    out["files"] = files
    return json.dumps(out, sort_keys=True)


def _sandbox_with_lines() -> int:
    rid = sandbox.create()
    draft.upsert_lines(rid, [
        {"line_no": 1, "description": "Laptop 14 inch", "canonical_uom": "piece",
         "annual_qty": "40", "spec": {"ram": "16 GB"}},
        {"line_no": 2, "description": "USB-C dock", "canonical_uom": "piece",
         "annual_qty": "40"}])
    sandbox.open_for_replies(rid)
    return rid


def _fake_reading(rid: int, message_id: int, image: bool = False) -> None:
    """What a finished reading leaves behind, without calling the model."""
    _jobs_done()
    with db.begin() as conn:
        vendor_id = conn.execute(sa.select(db.message.c.vendor_id).where(
            db.message.c.id == message_id)).scalar()
        att = conn.execute(sa.select(db.attachment.c.id).where(
            db.attachment.c.message_id == message_id)).scalar()
        line = conn.execute(sa.select(db.rfx_line.c.id).where(
            db.rfx_line.c.rfx_id == rid)).scalar()
        run_id = conn.execute(db.extraction_run.insert().values(
            attachment_id=att, message_id=message_id, stage="extract")).inserted_primary_key[0]
        img = None
        if image:
            img = db.upload_dir() / "derived" / "abc_quote.png"
            img.parent.mkdir(parents=True, exist_ok=True)
            img.write_bytes(b"png")
            (img.parent / "abc_quote_full.png").write_bytes(b"png full")
        conn.execute(db.source_block.insert().values(
            attachment_id=att, unit="image" if image else "line", idx=1, label="line 1",
            text="Laptop 52,000", image_path=str(img) if img else None))
        claim = conn.execute(db.quote_claim.insert().values(
            rfx_id=rid, vendor_id=vendor_id, run_id=run_id, raw_price="52000",
            evidence_quote="Laptop 52,000")).inserted_primary_key[0]
        conn.execute(db.claim_line.insert().values(claim_id=claim, rfx_line_id=line))
        conn.execute(db.cell.insert().values(
            rfx_id=rid, vendor_id=vendor_id, rfx_line_id=line, state="confirmed",
            claim_id=claim, unit_price_inr="52000"))
        flag = conn.execute(db.flag.insert().values(
            rfx_id=rid, vendor_id=vendor_id, run_id=run_id, target_kind="claim",
            target_id=claim, type="missing_info", severity="info",
            reason="freight not stated")).inserted_primary_key[0]
        conn.execute(db.audit_event.insert().values(
            actor="buyer", action="flag.confirm", target_kind="flag", target_id=flag,
            at=db.now_iso()))
        conn.execute(db.vendor_term.insert().values(
            rfx_id=rid, vendor_id=vendor_id, run_id=run_id, type="payment", text="30 days"))
        conn.execute(db.scenario.insert().values(rfx_id=rid, name="All to one", params={}))
        thread = conn.execute(db.thread.insert().values(kind="analyst", rfx_id=rid)
                              ).inserted_primary_key[0]
        conn.execute(db.chat_message.insert().values(thread_id=thread, role="user",
                                                     content={"text": "Who is lowest?"}))
        conn.execute(db.tool_call.insert().values(thread_id=thread, name="query_data"))


def _jobs_done() -> None:
    with db.begin() as conn:
        conn.execute(db.job.update().values(status="done"))


# --- Line lists ----------------------------------------------------------------------------


def test_line_list_with_a_header_and_unknown_columns():
    rows = line_list.rows_from_text("Item\tUnit\tAnnual qty\tBrand\tTarget price\n"
                                    "Laptop 14 inch\tpiece\t1,200\tAny\t52000\n"
                                    "Mouse\tpiece\t300\t\t400\n")
    lines, notes = line_list.parse(rows)
    assert [ln["description"] for ln in lines] == ["Laptop 14 inch", "Mouse"]
    assert lines[0]["annual_qty"] == "1200" and lines[0]["canonical_uom"] == "piece"
    assert lines[0]["spec"] == {"brand": "Any"}  # unknown column kept as an attribute
    assert all("52000" not in json.dumps(ln) for ln in lines)  # prices never copied to lines
    assert any("Price columns" in n for n in notes)
    assert [ln["line_no"] for ln in lines] == [1, 2]


def test_line_list_without_a_header_reads_description_unit_quantity():
    lines, notes = line_list.parse(line_list.rows_from_text(
        "Monitor 24 inch, piece, 50\nKeyboard, piece, lots\nCable"))
    assert lines[0] == {"spec": {}, "description": "Monitor 24 inch", "canonical_uom": "piece",
                        "annual_qty": "50", "line_no": 1}
    assert lines[1]["annual_qty"] is None and any("not a number" in n for n in notes)
    assert any("Line 3" in n and "no unit" in n for n in notes)


def test_line_list_from_excel_and_unsupported_files():
    import io

    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["No.", "Description", "UOM", "Qty"])
    wb.active.append([7, "Docking station", "piece", 40])
    buf = io.BytesIO()
    wb.save(buf)
    lines, _ = line_list.parse(line_list.rows_from_file(buf.getvalue(), "lines.xlsx"))
    assert lines == [{"spec": {}, "line_no": 7, "description": "Docking station",
                      "canonical_uom": "piece", "annual_qty": "40"}]
    with pytest.raises(intake.Unreadable):
        line_list.rows_from_file(b"%PDF-1.4 ...", "lines.pdf")


# --- Keeping the sandbox apart -------------------------------------------------------------


def test_sandbox_is_never_the_default_or_a_past_event(demo_event):
    before = data.current_rfx(None)["id"]
    rid = _sandbox_with_lines()
    assert data.current_rfx(None)["id"] == before  # a new visitor still lands on the demo
    assert data.current_rfx(rid)["id"] == rid  # the visitor who picked it sees it
    assert rid in [e["id"] for e in data.events()]  # listed in the picker
    assert draft.current_draft() is None or draft.current_draft()["id"] != rid
    assert rid not in [e["id"] for e in draft.history_sources(before)]
    with db.begin() as conn:
        conn.execute(db.rfx.update().where(db.rfx.c.id == rid).values(status="awarded"))
    assert rid not in [e["id"] for e in draft.past_events()]


def test_sandbox_takes_no_number_from_the_buyers_numbering(demo_event):
    with db.begin() as conn:
        conn.execute(db.rfx.update().where(db.rfx.c.id == demo_event).values(
            terms={"ref": "SF/IT/FY26/014"}))
    rid = sandbox.create()
    assert draft.snapshot(rid)["terms"]["ref"] == f"SANDBOX-{rid}"
    with db.begin() as conn:
        assert draft.next_ref(conn) == "SF/IT/FY26/015"


def test_sandbox_draft_needs_units_before_it_takes_quotes(demo_event):
    rid = sandbox.create()
    assert draft.current_draft(rid)["id"] == rid
    with pytest.raises(ValueError):
        sandbox.open_for_replies(rid)
    draft.upsert_lines(rid, [{"description": "Toner", "annual_qty": "10"}])
    with pytest.raises(ValueError, match="unit"):
        sandbox.open_for_replies(rid)
    with pytest.raises(sandbox.Limit, match="RFx page"):
        data.add_reply(rid, None, "Acme", [], "price 10")
    draft.upsert_lines(rid, [{"line_no": 1, "canonical_uom": "cartridge"}])
    sandbox.open_for_replies(rid)
    assert not any("No vendors invited" in i or "reply-by" in i for i in
                   draft.check_completeness(rid))  # a sandbox is never sent


def test_sandbox_caps_files_sizes_readings_and_questions(demo_event, monkeypatch):
    rid = _sandbox_with_lines()
    big = data.Upload("big.pdf", b"x" * (sandbox.MAX_FILE_MB * 1024 * 1024 + 1))
    with pytest.raises(sandbox.Limit, match="MB"):
        data.add_reply(rid, None, "Acme", [big], None)
    files = [data.Upload(f"q{i}.txt", f"price {i}".encode()) for i in range(sandbox.MAX_FILES)]
    data.add_reply(rid, None, "Acme", files, None)
    with pytest.raises(sandbox.Limit, match="none are left"):
        data.add_reply(rid, None, "Beta", [data.Upload("one.txt", b"more")], None)
    msg = data.messages(rid, "in")[0]["id"]
    monkeypatch.setattr(sandbox, "MAX_READS", 2)
    data.retry_message(msg)  # 1 reading from add_reply + this one = 2
    with pytest.raises(sandbox.Limit, match="read"):
        data.retry_message(msg)
    monkeypatch.setattr(sandbox, "MAX_CHAT", 0)
    with pytest.raises(sandbox.Limit):
        sandbox.check_chat(rid)
    sandbox.check_chat(demo_event)  # real events have no cap
    data.add_reply(demo_event, None, "Gamma", files, None)  # nor do their uploads


def test_sandbox_cannot_change_shared_assumptions_or_be_saved_as_the_demo(demo_event):
    rid = _sandbox_with_lines()
    with pytest.raises(sandbox.Limit):
        data.set_assumption("fx.USDINR", "90", "test", None, rid)
    with pytest.raises(demo.Busy, match="sandbox"):
        demo.save()


def test_at_most_a_few_sandboxes_open(demo_event):
    for _ in range(sandbox.MAX_SANDBOXES):
        sandbox.create()
    with pytest.raises(sandbox.Limit):
        sandbox.create()


# --- Delete --------------------------------------------------------------------------------


def test_delete_removes_the_sandbox_and_leaves_everything_else_byte_for_byte(demo_event):
    before = _dump()
    rid = _sandbox_with_lines()
    data.add_reply(rid, None, "Acme Computers", [data.Upload("acme.txt", b"Laptop 52,000")],
                   "Please find our quote.")
    msg = data.messages(rid, "in")[0]["id"]
    _fake_reading(rid, msg, image=True)
    assert _dump() != before
    gone = sandbox.delete(rid)
    assert gone["replies"] == 1 and gone["files"] == 1 and gone["vendors"] == 1
    assert gone["files_removed"] == 3  # the quote, its prepared image and the full image
    assert _dump() == before
    with pytest.raises(ValueError):
        sandbox.delete(demo_event)  # only a sandbox can be deleted


def test_delete_keeps_a_file_and_a_vendor_another_event_still_uses(demo_event):
    with db.connect() as conn:
        att = conn.execute(sa.select(db.attachment).where(
            db.attachment.c.kind == "quote")).mappings().first()
        demo_vendor = conn.execute(sa.select(db.invitation.c.vendor_id).where(
            db.invitation.c.rfx_id == demo_event)).scalars().first()
    from pathlib import Path

    same_bytes = Path(att["path"]).read_bytes()
    before = _dump()
    rid = _sandbox_with_lines()
    draft.set_invited(rid, [demo_vendor])  # e.g. copied in from a past event
    data.add_reply(rid, demo_vendor, None, [data.Upload("again" + Path(att["path"]).suffix,
                                                        same_bytes)], None)
    _jobs_done()
    sandbox.delete(rid)
    assert Path(att["path"]).exists()
    assert _dump() == before


def test_delete_waits_for_a_reading_in_progress(demo_event):
    rid = _sandbox_with_lines()
    data.add_reply(rid, None, "Acme", [], "Laptop 52,000")
    with pytest.raises(sandbox.Busy):
        sandbox.delete(rid)  # the reading job is still queued
    _jobs_done()
    sandbox.delete(rid)
    assert not sandbox.is_sandbox(rid)
    assert run.JOB_KIND == "pipeline.process_message"  # the kind the caps and delete count


# --- The Review badge counts what the Review queue shows ------------------------------------


def test_review_badge_counts_a_folded_point_once(demo_event):
    from components import review_data

    rid = _sandbox_with_lines()
    data.add_reply(rid, None, "Acme", [], "Laptop 52,000 per box")
    _jobs_done()
    with db.begin() as conn:
        vendor = conn.execute(sa.select(db.message.c.vendor_id).where(
            db.message.c.rfx_id == rid)).scalar()
        line = conn.execute(sa.select(db.rfx_line.c.id).where(
            db.rfx_line.c.rfx_id == rid, db.rfx_line.c.line_no == 1)).scalar()
        for kind, reason in [("plausibility", "price is 12x the median of other quotes"),
                             ("missing_info", "unit assumed: 1 box = 1 piece")]:
            conn.execute(db.flag.insert().values(
                rfx_id=rid, vendor_id=vendor, rfx_line_id=line, target_kind="cell",
                type=kind, severity="warning", reason=reason))
    left = sum(1 for i in review_data.issues(rid) if not i.decided)
    assert left == 1  # the unit point is folded into the price point
    assert data.open_issue_count(rid) == left
