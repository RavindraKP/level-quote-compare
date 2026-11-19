"""Install the analyst's read-only views (views.sql) into the database."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from level import db

SQL_FILE = Path(__file__).with_name("views.sql")


def statements() -> list[str]:
    """The statements in views.sql, split on the ;; marker."""
    text = SQL_FILE.read_text(encoding="utf-8")
    parts = [p.strip() for p in text.split(";;")]
    return [p for p in parts if p and not all(ln.startswith("--") for ln in p.splitlines())]


def install(conn: sa.Connection | None = None) -> None:
    """Drop and recreate every view. Safe to call repeatedly."""
    if conn is None:
        with db.begin() as c:
            return install(c)
    for stmt in statements():
        conn.exec_driver_sql(stmt)
