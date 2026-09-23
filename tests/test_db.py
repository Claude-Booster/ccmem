import sqlite3
import pytest
from ccmem.db import connect, migrate


def test_connect_returns_connection(tmp_path):
    con = connect(tmp_path / "mem.db")
    assert isinstance(con, sqlite3.Connection)
    con.close()


def test_migrate_creates_memories_table(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "memories" in tables
    assert "candidates" in tables
    assert "session_injections" in tables
    assert "schema_meta" in tables
    con.close()


def test_migrate_creates_fts5_index(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    vtables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchall()}
    assert "memories_fts" in vtables
    con.close()


def test_migrate_seeds_schema_meta(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    meta = dict(con.execute("SELECT key, value FROM schema_meta").fetchall())
    assert "schema_version" in meta
    assert "embedding_dim" in meta
    con.close()


def test_migrate_is_idempotent(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    migrate(con)  # must not raise
    con.close()


def test_memories_columns_match_design(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(memories)").fetchall()}
    required = {
        "id", "type", "content", "context", "subject",
        "scope", "project_id", "project_root",
        "created_at", "accessed_at", "access_count",
        "status", "supersedes", "embedding",
    }
    assert required <= cols, f"missing: {required - cols}"
    con.close()


def test_wal_pragma_set(tmp_path):
    con = connect(tmp_path / "mem.db")
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    con.close()
