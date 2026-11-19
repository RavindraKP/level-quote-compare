"""Try it with your own files: a separate event a visitor can fill with their own RFx lines and
vendor quotes, then delete with every file it holds.

A sandbox is an ordinary event row with `rfx.is_sandbox` set, so the RFx, Inbox, Review,
Compare, Analyst and Award code runs on it unchanged. What keeps it apart:
- it is never the default event, never a "past event" to copy from, and takes no number from
  the buyer's own numbering;
- it cannot change shared inputs (exchange rate, GST): those belong to every event;
- uploads, readings and chat turns are capped per sandbox, because every one costs API credit;
- `delete` removes the event and everything that hangs off it (rows and files), and nothing
  that any other event still uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa

from level import db

TITLE = "Sandbox: your own files"
MAX_SANDBOXES = 3  # open at the same time, across all visitors
MAX_FILES = 10  # vendor files per sandbox
MAX_FILE_MB = 10  # per file
MAX_READS = 15  # times replies are read (each reading calls the model)
MAX_CHAT = 25  # co-pilot and analyst questions together


class Limit(ValueError):
    """A sandbox cap was reached. The message is shown to the visitor as it is."""


class Busy(RuntimeError):
    """A reply in this sandbox is still being read; deleting now would cut it off."""


# --- Which events are sandboxes ------------------------------------------------------------


def ids() -> set[int]:
    with db.connect() as conn:
        return set(conn.execute(sa.select(db.rfx.c.id).where(db.rfx.c.is_sandbox)).scalars())


def is_sandbox(rfx_id: int | None) -> bool:
    if rfx_id is None:
        return False
    with db.connect() as conn:
        return bool(conn.execute(sa.select(db.rfx.c.is_sandbox).where(
            db.rfx.c.id == rfx_id)).scalar())


def create() -> int:
    """A new, empty sandbox event in draft. Raises Limit when too many are open."""
    with db.begin() as conn:
        n = conn.execute(sa.select(sa.func.count()).select_from(db.rfx).where(
            db.rfx.c.is_sandbox)).scalar()
        if n >= MAX_SANDBOXES:
            raise Limit(f"{n} sandboxes are open already, the most this site allows. Open one "
                        "from the event list in the sidebar, or delete one first.")
        rfx_id = conn.execute(db.rfx.insert().values(
            title=TITLE, status="draft", is_sandbox=True, terms={})).inserted_primary_key[0]
        conn.execute(db.rfx.update().where(db.rfx.c.id == rfx_id).values(
            terms={"ref": f"SANDBOX-{rfx_id}"}))
        conn.execute(db.audit_event.insert().values(
            actor="buyer", action="rfx.created", target_kind="rfx", target_id=rfx_id,
            after={"title": TITLE, "sandbox": True}, at=db.now_iso()))
    return rfx_id


def open_for_replies(rfx_id: int) -> None:
    """Lines are set: the sandbox starts taking replies. No email goes anywhere."""
    with db.begin() as conn:
        row = conn.execute(sa.select(db.rfx).where(db.rfx.c.id == rfx_id)).mappings().one()
        if not row["is_sandbox"]:
            raise ValueError("Only a sandbox can be opened this way.")
        if row["status"] != "draft":
            return
        lines = conn.execute(sa.select(db.rfx_line.c.description, db.rfx_line.c.canonical_uom)
                             .where(db.rfx_line.c.rfx_id == rfx_id)).all()
        if not lines:
            raise ValueError("Add at least one line first.")
        if any(not (u or "").strip() for _d, u in lines):
            raise ValueError("Every line needs a unit before quotes can be compared.")
        conn.execute(db.rfx.update().where(db.rfx.c.id == rfx_id).values(
            status="collecting", issued_at=db.now_iso()))
        conn.execute(db.audit_event.insert().values(
            actor="buyer", action="rfx.sent", target_kind="rfx", target_id=rfx_id,
            after={"vendors": 0, "sandbox": True}, at=db.now_iso()))


# --- Caps ----------------------------------------------------------------------------------


def _message_ids(conn, rfx_id: int) -> list[int]:
    return list(conn.execute(sa.select(db.message.c.id).where(
        db.message.c.rfx_id == rfx_id)).scalars())


def usage(rfx_id: int) -> dict:
    """What this sandbox has used so far: files, readings and chat questions."""
    with db.connect() as conn:
        msgs = _message_ids(conn, rfx_id)
        files = conn.execute(sa.select(sa.func.count()).select_from(db.attachment).where(
            db.attachment.c.message_id.in_(msgs or [-1]),
            db.attachment.c.kind != "other")).scalar()
        reads = conn.execute(sa.select(sa.func.count()).select_from(db.job).where(
            db.job.c.kind == "pipeline.process_message",
            db.job.c.target.in_([str(m) for m in msgs] or ["-1"]))).scalar()
        threads = conn.execute(sa.select(db.thread.c.id).where(
            db.thread.c.rfx_id == rfx_id, db.thread.c.kind.in_(["copilot", "analyst"]))
        ).scalars().all()
        chat = conn.execute(sa.select(sa.func.count()).select_from(db.chat_message).where(
            db.chat_message.c.thread_id.in_(threads or [-1]),
            db.chat_message.c.role == "user")).scalar()
    return {"files": files, "reads": reads, "chat": chat}


def check_upload(rfx_id: int, sizes: list[int]) -> None:
    """Raise Limit if these files would go over the sandbox caps. No-op for other events."""
    if not is_sandbox(rfx_id):
        return
    with db.connect() as conn:
        status = conn.execute(sa.select(db.rfx.c.status).where(db.rfx.c.id == rfx_id)).scalar()
    if status == "draft":
        raise Limit("Set your lines on the RFx page first and press \"Start adding quotes\"; "
                    "then add vendor replies here.")
    big = [s for s in sizes if s > MAX_FILE_MB * 1024 * 1024]
    if big:
        raise Limit(f"Files in the sandbox can be up to {MAX_FILE_MB} MB each.")
    used = usage(rfx_id)
    if used["files"] + len(sizes) > MAX_FILES:
        left = MAX_FILES - used["files"]
        raise Limit(f"The sandbox takes up to {MAX_FILES} files; "
                    + (f"{left} more can be added." if left > 0 else "none are left. Delete this "
                       "sandbox and start a new one to try more."))
    if used["reads"] >= MAX_READS:
        raise Limit(_reads_used())


def _reads_used() -> str:
    return (f"Replies in this sandbox have been read {MAX_READS} times, the most it allows. "
            "Delete this sandbox and start a new one to try more.")


def check_read(rfx_id: int) -> None:
    """Raise Limit if reading one more reply would go over the cap. No-op for other events."""
    if is_sandbox(rfx_id) and usage(rfx_id)["reads"] >= MAX_READS:
        raise Limit(_reads_used())


def check_chat(rfx_id: int) -> None:
    """Raise Limit if one more co-pilot or analyst question would go over the cap."""
    if is_sandbox(rfx_id) and usage(rfx_id)["chat"] >= MAX_CHAT:
        raise Limit(f"This sandbox has had {MAX_CHAT} questions, the most it allows. Delete "
                    "it and start a new one to ask more.")


# --- Delete --------------------------------------------------------------------------------


def _ids(conn, table: sa.Table, *where) -> list[int]:
    return list(conn.execute(sa.select(table.c.id).where(*where)).scalars())


def _in(column, values: list[int]):
    return column.in_(values or [-1])


def delete(rfx_id: int) -> dict:
    """Remove a sandbox event, its replies, readings, chats, vendors only it used, and its
    files. Returns what was removed. Refuses anything that is not a sandbox."""
    if not is_sandbox(rfx_id):
        raise ValueError("Only a sandbox can be deleted.")
    with db.begin() as conn:
        R = rfx_id
        lines = _ids(conn, db.rfx_line, db.rfx_line.c.rfx_id == R)
        msgs = _message_ids(conn, R)
        replies = conn.execute(sa.select(sa.func.count()).select_from(db.message).where(
            _in(db.message.c.id, msgs), db.message.c.direction == "in")).scalar()
        atts = _ids(conn, db.attachment, _in(db.attachment.c.message_id, msgs))
        jobs = _job_ids(conn, R, msgs)
        busy = conn.execute(sa.select(sa.func.count()).select_from(db.job).where(
            _in(db.job.c.id, jobs), db.job.c.status.in_(["queued", "running"]))).scalar()
        if busy:
            raise Busy("A reply in this sandbox is still being read. Wait for it to finish, "
                       "then delete.")
        runs = _ids(conn, db.extraction_run, sa.or_(
            _in(db.extraction_run.c.attachment_id, atts),
            _in(db.extraction_run.c.message_id, msgs)))
        claims = _ids(conn, db.quote_claim, db.quote_claim.c.rfx_id == R)
        flags = _ids(conn, db.flag, db.flag.c.rfx_id == R)
        scenarios = _ids(conn, db.scenario, db.scenario.c.rfx_id == R)
        cells = _ids(conn, db.cell, db.cell.c.rfx_id == R)
        terms = _ids(conn, db.vendor_term, db.vendor_term.c.rfx_id == R)
        answers = _ids(conn, db.answer, db.answer.c.rfx_id == R)
        questions = _ids(conn, db.question, db.question.c.rfx_id == R)
        threads = _ids(conn, db.thread, db.thread.c.rfx_id == R)
        vendors = set(conn.execute(sa.select(db.invitation.c.vendor_id).where(
            db.invitation.c.rfx_id == R)).scalars())
        vendors |= set(conn.execute(sa.select(db.message.c.vendor_id).where(
            _in(db.message.c.id, msgs))).scalars()) - {None}
        files = conn.execute(sa.select(db.attachment.c.path).where(
            _in(db.attachment.c.id, atts))).scalars().all()
        images = conn.execute(sa.select(db.source_block.c.image_path).where(sa.or_(
            _in(db.source_block.c.attachment_id, atts), _in(db.source_block.c.message_id, msgs)),
            db.source_block.c.image_path.is_not(None))).scalars().all()
        audits = _audit_ids(conn, R, {
            "rfx": [R], "rfx_line": lines, "message": msgs, "attachment": atts, "flag": flags,
            "scenario": scenarios, "cell": cells, "claim": claims, "term": terms,
            "answer": answers, "question": questions, "thread": threads})

        # Children before parents (foreign keys are enforced).
        conn.execute(db.audit_event.delete().where(_in(db.audit_event.c.id, audits)))
        conn.execute(db.tool_call.delete().where(_in(db.tool_call.c.thread_id, threads)))
        conn.execute(db.chat_message.delete().where(_in(db.chat_message.c.thread_id, threads)))
        conn.execute(db.thread.delete().where(_in(db.thread.c.id, threads)))
        conn.execute(db.scenario.delete().where(db.scenario.c.rfx_id == R))
        conn.execute(db.cell.delete().where(db.cell.c.rfx_id == R))
        conn.execute(db.flag.delete().where(db.flag.c.rfx_id == R))
        conn.execute(db.answer.delete().where(db.answer.c.rfx_id == R))
        conn.execute(db.claim_line.delete().where(sa.or_(
            _in(db.claim_line.c.claim_id, claims), _in(db.claim_line.c.rfx_line_id, lines))))
        conn.execute(db.quote_claim.delete().where(db.quote_claim.c.rfx_id == R))
        conn.execute(db.vendor_term.delete().where(db.vendor_term.c.rfx_id == R))
        conn.execute(db.source_block.delete().where(sa.or_(
            _in(db.source_block.c.attachment_id, atts), _in(db.source_block.c.message_id, msgs))))
        conn.execute(db.extraction_run.delete().where(_in(db.extraction_run.c.id, runs)))
        conn.execute(db.job.delete().where(_in(db.job.c.id, jobs)))
        conn.execute(db.attachment.delete().where(_in(db.attachment.c.id, atts)))
        conn.execute(db.message.delete().where(_in(db.message.c.id, msgs)))
        conn.execute(db.award_line.delete().where(db.award_line.c.rfx_id == R))
        conn.execute(db.price_history.delete().where(_in(db.price_history.c.rfx_line_id, lines)))
        conn.execute(db.question.delete().where(db.question.c.rfx_id == R))
        conn.execute(db.invitation.delete().where(db.invitation.c.rfx_id == R))
        conn.execute(db.rfx_line.delete().where(db.rfx_line.c.rfx_id == R))
        conn.execute(db.rfx.delete().where(db.rfx.c.id == R))
        gone_vendors = [v for v in vendors if not _vendor_in_use(conn, v)]
        conn.execute(db.vendor.delete().where(_in(db.vendor.c.id, gone_vendors)))
        # Files are named by their content, so another event may hold the same file.
        still_files = set(conn.execute(sa.select(db.attachment.c.path)).scalars())
        still_images = set(conn.execute(sa.select(db.source_block.c.image_path)).scalars())
    removed = _remove_files([p for p in set(files) if p not in still_files])
    removed += _remove_files([p for p in set(images) if p not in still_images], derived=True)
    return {"replies": replies, "files": len(atts), "lines": len(lines),
            "vendors": len(gone_vendors), "files_removed": removed}


def _job_ids(conn, rfx_id: int, msgs: list[int]) -> list[int]:
    """Reading jobs for the sandbox's replies, and analyst jobs for its questions."""
    ids = _ids(conn, db.job, db.job.c.kind == "pipeline.process_message",
               db.job.c.target.in_([str(m) for m in msgs] or ["-1"]))
    for job_id, target in conn.execute(sa.select(db.job.c.id, db.job.c.target).where(
            db.job.c.kind == "analyst.ask")):
        try:
            if json.loads(target).get("rfx_id") == rfx_id:
                ids.append(job_id)
        except (ValueError, AttributeError):
            continue
    return ids


def _audit_ids(conn, rfx_id: int, targets: dict[str, list[int]]) -> list[int]:
    """Audit entries about the sandbox: their target is one of its rows, or a reply-link
    answer names it. Changes to shared inputs (assumptions) are never removed."""
    from level.agents.analyst import audit  # the event's own trail rules (UX audit B8)

    ctx = audit._context(conn, rfx_id)
    wanted = {(kind, i) for kind, values in targets.items() for i in values}
    out = []
    for e in conn.execute(sa.select(db.audit_event)).mappings():
        e = dict(e)
        if e["target_kind"] == "assumption":
            continue
        if (e["target_kind"], e["target_id"]) in wanted or audit.belongs(e, ctx):
            out.append(e["id"])
    return out


_VENDOR_REFS = [
    (db.invitation, "vendor_id"), (db.message, "vendor_id"), (db.attachment, "vendor_id"),
    (db.quote_claim, "vendor_id"), (db.vendor_term, "vendor_id"), (db.cell, "vendor_id"),
    (db.answer, "vendor_id"), (db.flag, "vendor_id"), (db.price_history, "vendor_id"),
    (db.award_line, "vendor_id"), (db.thread, "vendor_id"), (db.rfx_line, "incumbent_vendor_id"),
]


def _vendor_in_use(conn, vendor_id: int) -> bool:
    """Does any row that is left still point at this vendor?"""
    return any(conn.execute(sa.select(sa.func.count()).select_from(t).where(
        t.c[col] == vendor_id)).scalar() for t, col in _VENDOR_REFS)


def _remove_files(paths: list[str], derived: bool = False) -> int:
    """Delete stored files that only the sandbox used. Only inside the app's own folders."""
    from level.pipeline import vision

    roots = [db.upload_dir().resolve(), vision.derived_dir().resolve()]
    n = 0
    for p in paths:
        targets = [Path(p)]
        if derived:  # the full-resolution copy kept next to each prepared image
            stem = Path(p).with_suffix("").name
            targets.append(Path(p).with_name(f"{stem}_full.png"))
        for t in targets:
            try:
                resolved = t.resolve()
                if any(resolved.is_relative_to(r) for r in roots) and resolved.is_file():
                    resolved.unlink()
                    n += 1
            except OSError:
                continue  # a file in use or already gone: the rows are removed either way
    return n
