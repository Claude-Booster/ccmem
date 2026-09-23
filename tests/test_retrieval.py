from ccmem.db import connect, migrate
from ccmem.retrieval import Memory, retrieve, mark_accessed

PROJECT_ID = "aabbccdd11223344"


def seeded_con(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    con.execute("""
        INSERT INTO memories (id, type, content, context, subject, scope,
                              project_id, project_root, created_at, status)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, ("m1", "decision", "chose RLS triggers over app-layer limits",
          "latency under contention", "rate-limiter", "project",
          PROJECT_ID, "/repo", "2026-09-01T00:00:00Z", "active"))
    con.execute("""
        INSERT INTO memories (id, type, content, context, subject, scope,
                              project_id, project_root, created_at, status)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, ("m2", "preference", "prefers targeted patch edits over full rewrites",
          None, "editing", "project",
          PROJECT_ID, "/repo", "2026-09-15T00:00:00Z", "active"))
    con.execute("""
        INSERT INTO memories (id, type, content, context, subject, scope,
                              project_id, project_root, created_at, status)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, ("m3", "gotcha", "port 8076 is closed; use SSH tunnel to 8077",
          None, "port-8076", "global",
          "other_project", "/other", "2026-09-10T00:00:00Z", "active"))
    con.commit()
    con.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    con.commit()
    return con


def test_retrieve_all_returns_project_and_global(tmp_path):
    con = seeded_con(tmp_path)
    mems = retrieve(con, PROJECT_ID)
    ids = {m.id for m in mems}
    assert "m1" in ids
    assert "m2" in ids
    assert "m3" in ids  # global scope crosses projects
    con.close()


def test_retrieve_excludes_superseded(tmp_path):
    con = seeded_con(tmp_path)
    con.execute("UPDATE memories SET status='superseded' WHERE id='m1'")
    con.commit()
    mems = retrieve(con, PROJECT_ID)
    assert all(m.id != "m1" for m in mems)
    con.close()


def test_retrieve_fts_filters_by_query(tmp_path):
    con = seeded_con(tmp_path)
    mems = retrieve(con, PROJECT_ID, query="RLS triggers")
    assert any(m.id == "m1" for m in mems)
    con.close()


def test_retrieve_respects_top_k(tmp_path):
    con = seeded_con(tmp_path)
    mems = retrieve(con, PROJECT_ID, top_k=1)
    assert len(mems) <= 1
    con.close()


def test_mark_accessed_increments_count(tmp_path):
    con = seeded_con(tmp_path)
    mark_accessed(con, ["m1"])
    row = con.execute("SELECT access_count FROM memories WHERE id='m1'").fetchone()
    assert row[0] == 1
    con.close()
