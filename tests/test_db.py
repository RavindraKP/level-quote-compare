import pytest
import sqlalchemy as sa

from level import db


def test_reset_creates_every_table(tmp_db):
    db.reset()
    names = set(sa.inspect(db.engine()).get_table_names())
    expected = {
        "rfx", "rfx_line", "question", "vendor", "invitation", "price_history", "message",
        "attachment", "source_block", "extraction_run", "quote_claim", "claim_line",
        "vendor_term", "cell", "answer", "flag", "assumption", "audit_event", "thread",
        "chat_message", "tool_call", "scenario", "job",
    }
    assert expected <= names


def test_connections_use_wal_and_busy_timeout(tmp_db):
    with db.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == db.BUSY_TIMEOUT_MS


def test_readonly_connection_reads_but_cannot_write(tmp_db):
    with db.begin() as conn:
        conn.execute(db.vendor.insert().values(name="Acme"))
    with db.connect_readonly() as ro:
        assert ro.execute(sa.text("SELECT count(*) FROM vendor")).scalar() == 1
        with pytest.raises(sa.exc.OperationalError):
            ro.execute(sa.text("INSERT INTO vendor (name, is_incumbent, created_at)"
                               " VALUES ('x', 0, 'now')"))
