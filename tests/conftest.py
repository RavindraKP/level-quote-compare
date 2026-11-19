import pytest

from level import db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """A fresh database file per test, selected through LEVEL_DB_PATH."""
    path = tmp_path / "test.db"
    monkeypatch.setenv("LEVEL_DB_PATH", str(path))
    db.init()
    return path
