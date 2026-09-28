from ccmem.db import connect, migrate
from ccmem.supersession import maybe_supersede


def _insert(con, id, subject, project_id, status="active", scope="project"):
    con.execute("""
        INSERT INTO memories (id, type, content, subject, scope,
                              project_id, project_root, created_at, status)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (id, "decision", f"content for {id}", subject, scope,
          project_id, "/repo", "2026-01-01T00:00:00Z", status))
    con.commit()


def test_supersedes_matching_subject(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    _insert(con, "old", "rate-limiter", "proj1")
    _insert(con, "new", "rate-limiter", "proj1")
    superseded = maybe_supersede(con, "new", "rate-limiter", "proj1")
    assert superseded == "old"
    old = con.execute("SELECT status FROM memories WHERE id='old'").fetchone()
    assert old[0] == "superseded"
    con.close()


def test_no_supersession_when_no_match(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    _insert(con, "m1", "other-subject", "proj1")
    result = maybe_supersede(con, "m1", "rate-limiter", "proj1")
    assert result is None
    con.close()


def test_no_supersession_when_subject_is_none(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    _insert(con, "m1", None, "proj1")
    result = maybe_supersede(con, "m1", None, "proj1")
    assert result is None
    con.close()


def test_no_cross_project_supersession(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    _insert(con, "old", "rate-limiter", "proj1")
    _insert(con, "new", "rate-limiter", "proj2")
    result = maybe_supersede(con, "new", "rate-limiter", "proj2")
    assert result is None  # different project_id
    con.close()


def test_no_cross_scope_supersession(tmp_path):
    # A global memory and a project memory sharing a subject + project_id must NOT
    # clobber each other (the bug that wiped the global sentinel during wiring).
    con = connect(tmp_path / "mem.db")
    migrate(con)
    _insert(con, "g", "shared-subject", "proj1", scope="global")
    _insert(con, "p", "shared-subject", "proj1", scope="project")
    result = maybe_supersede(con, "p", "shared-subject", "proj1", scope="project")
    assert result is None
    assert con.execute("SELECT status FROM memories WHERE id='g'").fetchone()[0] == "active"
    con.close()


def test_global_supersedes_across_projects(tmp_path):
    # Global scope is a cross-project namespace: a newer global fact supersedes the
    # older one regardless of which project it was first recorded from.
    con = connect(tmp_path / "mem.db")
    migrate(con)
    _insert(con, "old", "tabs-vs-spaces", "projA", scope="global")
    _insert(con, "new", "tabs-vs-spaces", "projB", scope="global")
    result = maybe_supersede(con, "new", "tabs-vs-spaces", "projB", scope="global")
    assert result == "old"
    con.close()
