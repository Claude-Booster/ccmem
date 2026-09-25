import sqlite3
from ccmem.db import connect, migrate

def _cols(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}

def test_new_schema_objects_exist():
    con = connect(":memory:"); migrate(con)
    assert "content_hash" in _cols(con, "candidates")
    assert "content_hash" in _cols(con, "memories")
    assert "acknowledged_at" in _cols(con, "sigil_refusals")
    for t in ("transcript_progress", "sigil_refusals"):
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone(), f"{t} missing"
    cidx = {r[1] for r in con.execute("PRAGMA index_list(candidates)")}
    midx = {r[1] for r in con.execute("PRAGMA index_list(memories)")}
    assert "idx_candidates_content_hash" in cidx
    assert "idx_memories_content_hash" in midx
    assert con.execute(
        "SELECT value FROM schema_meta WHERE key='initialized_at'"
    ).fetchone() is not None

def test_busy_timeout_set():
    con = connect(":memory:")
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 3000

def test_content_hash_unique_dedups():
    con = connect(":memory:"); migrate(con)
    for i in (1, 2):
        try:
            con.execute(
                "INSERT INTO candidates (id, session_id, user_turn, assistant_turn,"
                " classifier_score, created_at, status, content_hash)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (f"id{i}", "s", "u", "a", 5.0, "t", "pending", "HASH"),
            )
        except sqlite3.IntegrityError:
            pass
    assert con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
