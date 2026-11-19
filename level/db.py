"""SQLite schema (PLAN §5) and connection factories.

Rules:
- Every caller opens its own connection (`with connect() as conn:`). Connections are never
  shared across threads: the engine uses NullPool, so each `connect()` is a fresh SQLite
  connection that is closed afterwards.
- Every connection runs in WAL mode with a busy timeout, so the worker thread and the page
  threads can read and write at the same time without "database is locked" errors.
- The analyst gets `connect_readonly()`, opened with SQLite's `mode=ro`: it cannot write.
- Money and quantities are stored as TEXT decimal strings (exact); code uses Decimal.

CLI: `python -m level.db reset` drops and recreates every table; `python -m level.db init`
creates missing tables only.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parent.parent
BUSY_TIMEOUT_MS = 10_000

metadata = sa.MetaData()


def now_iso() -> str:
    """UTC timestamp as a fixed-width ISO string, so string comparison orders correctly."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _id() -> sa.Column:
    return sa.Column("id", sa.Integer, primary_key=True, autoincrement=True)


def _fk(name: str, target: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Integer, sa.ForeignKey(target), nullable=nullable, index=True)


def _created() -> sa.Column:
    return sa.Column("created_at", sa.Text, nullable=False, default=now_iso)


def _evidence() -> list[sa.Column]:
    """Columns every fact carries: which file, where in it, and the verbatim words."""
    return [
        sa.Column("attachment_id", sa.Integer, sa.ForeignKey("attachment.id"), index=True),
        sa.Column("evidence_locator", sa.Text),
        sa.Column("evidence_quote", sa.Text),
        sa.Column("evidence_ok", sa.Boolean),  # T1 result; NULL = not checked yet
    ]


# --- The event -------------------------------------------------------------

rfx = sa.Table(
    "rfx", metadata, _id(),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("category", sa.Text),
    sa.Column("base_currency", sa.Text, nullable=False, default="INR"),
    # draft -> issued -> collecting -> ready -> awarded (PLAN §6 event lifecycle)
    sa.Column("status", sa.Text, nullable=False, default="draft"),
    sa.Column("terms", sa.JSON, default=dict),
    sa.Column("issued_at", sa.Text),
    # A "Try it with your own files" event (level/sandbox.py): kept apart from real events.
    sa.Column("is_sandbox", sa.Boolean, nullable=False, default=False, server_default="0"),
    _created(),
)

rfx_line = sa.Table(
    "rfx_line", metadata, _id(),
    _fk("rfx_id", "rfx.id", nullable=False),
    sa.Column("line_no", sa.Integer, nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("spec", sa.JSON, default=dict),  # any attributes; nothing assumes a category
    sa.Column("canonical_uom", sa.Text, nullable=False),
    sa.Column("annual_qty", sa.Text),  # decimal string
    sa.Column("conversion_factors", sa.JSON, default=dict),  # e.g. {"g_per_piece": "412"}
    sa.Column("plant", sa.Text),
    sa.Column("last_price", sa.Text),  # INR per canonical_uom, decimal string
    _fk("incumbent_vendor_id", "vendor.id"),
    sa.UniqueConstraint("rfx_id", "line_no"),
)

question = sa.Table(
    "question", metadata, _id(),
    _fk("rfx_id", "rfx.id", nullable=False),
    sa.Column("q_no", sa.Integer),
    sa.Column("section", sa.Text),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("answer_type", sa.Text),  # yes_no / number / date / text / file
    sa.Column("is_gating", sa.Boolean, nullable=False, default=False),
    sa.Column("pass_rule", sa.JSON),  # e.g. {"op": "<=", "value": "10"}
)

vendor = sa.Table(
    "vendor", metadata, _id(),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("email", sa.Text),
    sa.Column("is_incumbent", sa.Boolean, nullable=False, default=False),
    _created(),
)

# Which vendors are invited to which RFx, and whether they replied (drives "awaiting").
invitation = sa.Table(
    "invitation", metadata, _id(),
    _fk("rfx_id", "rfx.id", nullable=False),
    _fk("vendor_id", "vendor.id", nullable=False),
    sa.Column("status", sa.Text, nullable=False, default="invited"),  # invited/replied/no_response
    sa.UniqueConstraint("rfx_id", "vendor_id"),
)

# The buyer's price memory: each vendor's records from last time, per line.
price_history = sa.Table(
    "price_history", metadata, _id(),
    _fk("vendor_id", "vendor.id", nullable=False),
    _fk("rfx_line_id", "rfx_line.id"),
    sa.Column("item_text", sa.Text),
    sa.Column("period", sa.Text),  # e.g. "FY25"
    sa.Column("unit_price", sa.Text),  # decimal string
    sa.Column("currency", sa.Text),
    sa.Column("uom", sa.Text),
    sa.Column("source", sa.Text),  # "contract" (what the buyer paid) or "quote" (a losing bid)
)

# Who was awarded each line of an event that was decided outside Level (a past event loaded
# as a record). Events awarded inside Level keep their award as a scenario instead.
# unit_price is in the event's base currency per the line's canonical unit (decimal string).
award_line = sa.Table(
    "award_line", metadata, _id(),
    _fk("rfx_id", "rfx.id", nullable=False),
    _fk("rfx_line_id", "rfx_line.id", nullable=False),
    _fk("vendor_id", "vendor.id", nullable=False),
    sa.Column("unit_price", sa.Text),
    sa.UniqueConstraint("rfx_line_id"),
)

# --- Mail and files --------------------------------------------------------

message = sa.Table(
    "message", metadata, _id(),
    _fk("rfx_id", "rfx.id"),
    _fk("vendor_id", "vendor.id"),
    sa.Column("direction", sa.Text, nullable=False),  # in / out
    sa.Column("channel", sa.Text, nullable=False, default="email"),  # email/upload/reply_link
    sa.Column("status", sa.Text, nullable=False, default="received"),  # draft/sent/received
    sa.Column("subject", sa.Text),
    sa.Column("sender", sa.Text),
    sa.Column("recipient", sa.Text),
    sa.Column("body", sa.Text),
    sa.Column("received_at", sa.Text),
    _created(),
)

attachment = sa.Table(
    "attachment", metadata, _id(),
    _fk("message_id", "message.id"),
    _fk("vendor_id", "vendor.id"),
    sa.Column("filename", sa.Text, nullable=False),
    sa.Column("mime", sa.Text),
    sa.Column("kind", sa.Text, default="quote"),  # quote/certificate/report/other
    sa.Column("path", sa.Text, nullable=False),  # data/uploads/<sha256>
    sa.Column("sha256", sa.Text, nullable=False, index=True),
    sa.Column("size_bytes", sa.Integer),
    sa.Column("read_error", sa.Text),  # set when the file could not be read
    _created(),
)

# Layer 2: the readable form of each file, one row per addressable unit.
source_block = sa.Table(
    "source_block", metadata, _id(),
    _fk("attachment_id", "attachment.id"),
    _fk("message_id", "message.id"),  # for email bodies, which have no attachment
    sa.Column("unit", sa.Text, nullable=False),  # sheet/page/paragraph/table/email_body/image
    sa.Column("idx", sa.Integer, nullable=False),
    sa.Column("label", sa.Text),  # "Sheet2", "page 2", "paragraph 7"
    sa.Column("text", sa.Text),
    sa.Column("image_path", sa.Text),
)

# --- Pipeline output -------------------------------------------------------

extraction_run = sa.Table(
    "extraction_run", metadata, _id(),
    _fk("attachment_id", "attachment.id"),
    _fk("message_id", "message.id"),
    sa.Column("job_id", sa.Integer, index=True),
    sa.Column("stage", sa.Text, nullable=False),
    sa.Column("model", sa.Text),
    sa.Column("prompt_version", sa.Text),
    sa.Column("raw_output", sa.Text),
    sa.Column("input_tokens", sa.Integer),
    sa.Column("output_tokens", sa.Integer),
    sa.Column("latency_ms", sa.Integer),
    sa.Column("status", sa.Text, nullable=False, default="ok"),
    sa.Column("error", sa.Text),
    _created(),
)

quote_claim = sa.Table(
    "quote_claim", metadata, _id(),
    _fk("rfx_id", "rfx.id", nullable=False),
    _fk("vendor_id", "vendor.id", nullable=False),
    _fk("run_id", "extraction_run.id"),
    sa.Column("row_index", sa.Integer),
    sa.Column("vendor_item_text", sa.Text),
    sa.Column("raw_price", sa.Text),
    sa.Column("raw_currency", sa.Text),
    sa.Column("raw_uom_text", sa.Text),
    sa.Column("basis", sa.JSON),  # {"per": "piece", "multiple": "1000"}
    sa.Column("quantity_break", sa.JSON),
    sa.Column("scope", sa.Text, nullable=False, default="line"),  # line/rule/reference
    sa.Column("rule_text", sa.Text),
    sa.Column("read_candidates", sa.JSON, default=list),
    *_evidence(),
    _created(),
)

claim_line = sa.Table(
    "claim_line", metadata, _id(),
    _fk("claim_id", "quote_claim.id", nullable=False),
    _fk("rfx_line_id", "rfx_line.id", nullable=False),
    sa.Column("match_rationale", sa.Text),
    sa.Column("spec_deviation", sa.JSON, default=list),
    sa.Column("status", sa.Text, nullable=False, default="proposed"),
    sa.UniqueConstraint("claim_id", "rfx_line_id"),
)

vendor_term = sa.Table(
    "vendor_term", metadata, _id(),
    _fk("rfx_id", "rfx.id", nullable=False),
    _fk("vendor_id", "vendor.id", nullable=False),
    _fk("run_id", "extraction_run.id"),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("text", sa.Text),
    sa.Column("value", sa.Text),
    sa.Column("conditions", sa.Text),
    sa.Column("is_conditional", sa.Boolean, nullable=False, default=False),
    *_evidence(),
    _created(),
)

# Materialised grid. Rebuilt by level/core/cells.py only; nothing else writes here.
cell = sa.Table(
    "cell", metadata, _id(),
    _fk("rfx_id", "rfx.id", nullable=False),
    _fk("vendor_id", "vendor.id", nullable=False),
    _fk("rfx_line_id", "rfx_line.id", nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("reason", sa.Text),
    _fk("claim_id", "quote_claim.id"),
    sa.Column("as_quoted", sa.Text),
    sa.Column("unit_price_inr", sa.Text),
    sa.Column("landed_price_inr", sa.Text),
    sa.Column("conversion_steps", sa.JSON, default=list),
    sa.Column("assumptions", sa.JSON, default=dict),
    sa.Column("flag_ids", sa.JSON, default=list),
    sa.Column("confirmed_by", sa.Text),
    sa.Column("updated_at", sa.Text, default=now_iso),
    sa.UniqueConstraint("vendor_id", "rfx_line_id"),
)

answer = sa.Table(
    "answer", metadata, _id(),
    _fk("rfx_id", "rfx.id", nullable=False),
    _fk("question_id", "question.id"),
    _fk("vendor_id", "vendor.id", nullable=False),
    _fk("run_id", "extraction_run.id"),
    sa.Column("value", sa.Text),
    sa.Column("stance", sa.Text),  # yes / no / unclear: the reader's yes-no reading of value
    sa.Column("verdict", sa.Text),  # pass / fail / unclear (set by code)
    sa.Column("rationale", sa.Text),
    *_evidence(),
    _created(),
)

flag = sa.Table(
    "flag", metadata, _id(),
    _fk("rfx_id", "rfx.id", nullable=False),
    _fk("vendor_id", "vendor.id"),
    _fk("run_id", "extraction_run.id"),
    sa.Column("target_kind", sa.Text, nullable=False),
    sa.Column("target_id", sa.Integer),
    _fk("rfx_line_id", "rfx_line.id"),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("severity", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("suggested_action", sa.Text),
    sa.Column("candidates", sa.JSON, default=list),
    sa.Column("state", sa.Text, nullable=False, default="open"),
    sa.Column("closed_by", sa.Text),  # checks / buyer / vendor
    sa.Column("resolution", sa.Text),
    sa.Column("opened_at", sa.Text, default=now_iso),
    sa.Column("closed_at", sa.Text),
)

assumption = sa.Table(
    "assumption", metadata,
    sa.Column("key", sa.Text, primary_key=True),  # fx.USDINR, cost_of_capital, gst_rate
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("source", sa.Text),
    sa.Column("as_of", sa.Text),
    sa.Column("updated_at", sa.Text, default=now_iso),
)

audit_event = sa.Table(
    "audit_event", metadata, _id(),
    sa.Column("actor", sa.Text, nullable=False),  # system / buyer / vendor
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("target_kind", sa.Text),
    sa.Column("target_id", sa.Integer),
    sa.Column("before", sa.JSON),
    sa.Column("after", sa.JSON),
    sa.Column("at", sa.Text, nullable=False, default=now_iso),
)

# --- Chat ------------------------------------------------------------------

thread = sa.Table(
    "thread", metadata, _id(),
    sa.Column("kind", sa.Text, nullable=False),  # copilot / analyst / vendor_link
    _fk("rfx_id", "rfx.id"),
    _fk("vendor_id", "vendor.id"),
    sa.Column("title", sa.Text),
    _created(),
)

chat_message = sa.Table(
    "chat_message", metadata, _id(),
    _fk("thread_id", "thread.id", nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("content", sa.JSON),
    _created(),
)

tool_call = sa.Table(
    "tool_call", metadata, _id(),
    _fk("thread_id", "thread.id", nullable=False),
    _fk("message_id", "chat_message.id"),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("input", sa.JSON),
    sa.Column("output", sa.JSON),
    sa.Column("error", sa.Text),
    sa.Column("latency_ms", sa.Integer),
    _created(),
)

scenario = sa.Table(
    "scenario", metadata, _id(),
    _fk("rfx_id", "rfx.id", nullable=False),
    sa.Column("name", sa.Text),
    sa.Column("params", sa.JSON, nullable=False),
    sa.Column("result", sa.JSON),
    sa.Column("pinned", sa.Boolean, nullable=False, default=False),
    sa.Column("is_award", sa.Boolean, nullable=False, default=False),
    _created(),
)

# --- Background jobs (PLAN §5 "Background worker design") -----------------

job = sa.Table(
    "job", metadata, _id(),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("target", sa.Text, nullable=False),
    sa.Column("dedupe_key", sa.Text, nullable=False),  # kind:target:file_hash
    sa.Column("status", sa.Text, nullable=False, default="queued"),
    sa.Column("stage", sa.Text),
    sa.Column("progress", sa.Float, nullable=False, default=0.0),
    sa.Column("attempts", sa.Integer, nullable=False, default=0),
    sa.Column("error", sa.Text),
    sa.Column("heartbeat_at", sa.Text),
    _created(),
    sa.Column("started_at", sa.Text),
    sa.Column("finished_at", sa.Text),
)
# Only one unfinished job per key: a double-click or refresh finds the existing job.
sa.Index(
    "ux_job_unfinished", job.c.dedupe_key, unique=True,
    sqlite_where=job.c.status.in_(["queued", "running"]),
)
sa.Index("ix_job_status", job.c.status, job.c.id)


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


def db_path() -> Path:
    """Database file, from LEVEL_DB_PATH (default data/level.db)."""
    return Path(os.environ.get("LEVEL_DB_PATH", ROOT / "data" / "level.db"))


def upload_dir() -> Path:
    """Where original files are kept (layer 1), from LEVEL_UPLOAD_DIR (default data/uploads)."""
    path = Path(os.environ.get("LEVEL_UPLOAD_DIR", ROOT / "data" / "uploads"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _on_connect(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")  # readers and one writer at the same time
    cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")  # wait for a lock instead of failing
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


_engines: dict[str, sa.Engine] = {}


def engine() -> sa.Engine:
    """Read-write engine for the current db_path(). NullPool: one fresh connection per use."""
    path = db_path()
    key = str(path)
    if key not in _engines:
        path.parent.mkdir(parents=True, exist_ok=True)
        eng = sa.create_engine(
            f"sqlite:///{path}",
            poolclass=NullPool,
            connect_args={"timeout": BUSY_TIMEOUT_MS / 1000},
        )
        sa.event.listen(eng, "connect", _on_connect)
        _engines[key] = eng
    return _engines[key]


def connect() -> sa.Connection:
    """A normal connection. Use as `with connect() as conn:`; commit with conn.commit()."""
    return engine().connect()


def begin():
    """A connection inside a transaction that commits on exit: `with begin() as conn:`."""
    return engine().begin()


def _readonly_sqlite() -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{db_path()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000,
        check_same_thread=False,
    )
    conn.execute("PRAGMA query_only=ON")  # second lock on the door
    return conn


def readonly_engine() -> sa.Engine:
    """Engine whose connections cannot write (SQLite mode=ro). For the analyst's SQL tool."""
    return sa.create_engine("sqlite://", creator=_readonly_sqlite, poolclass=NullPool)


def connect_readonly() -> sa.Connection:
    return readonly_engine().connect()


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------


# Columns added after a database may already exist (a saved demo, a hosted disk).
_ADDED_COLUMNS = [("answer", "stance", "TEXT"),
                  ("rfx", "is_sandbox", "BOOLEAN NOT NULL DEFAULT 0")]


def init() -> None:
    """Create any missing tables and columns. Safe to call on every start."""
    eng = engine()
    metadata.create_all(eng)
    with eng.begin() as conn:
        for table, column, kind in _ADDED_COLUMNS:
            have = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in have:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    _install_views()


def _install_views() -> None:
    """The analyst's read-only views exist from the start, not only after a grid rebuild."""
    from level.core import views  # imported here: level.core imports this module

    views.install()


def reset() -> None:
    """Drop and recreate every table. Deletes all data."""
    eng = engine()
    with eng.begin() as conn:  # views first: drop_all does not know about them
        for (name,) in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'view'").all():
            conn.exec_driver_sql(f'DROP VIEW IF EXISTS "{name}"')
    metadata.drop_all(eng)
    metadata.create_all(eng)
    _install_views()


def _main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "init"
    if cmd == "reset":
        reset()
        print(f"Schema recreated at {db_path()}")
    elif cmd == "init":
        init()
        print(f"Schema ready at {db_path()}")
    else:
        print("usage: python -m level.db [reset|init]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
