import os
import sqlite3
import tempfile
from pathlib import Path


def _seed(con, memories):
    from ccmem.db import migrate
    migrate(con)
    for m in memories:
        con.execute(
            "INSERT INTO memories "
            "(id, type, content, subject, scope, project_id, project_root, "
            "created_at, status, access_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            m,
        )
    con.commit()


# --- Task 1: two-phase render, whole-file cap, scope isolation --------------


def test_render_empty_returns_stub():
    from ccmem.generate import _render
    out = _render([], 400)
    assert out.startswith("<!-- ccmem -->")
    assert "0 of 0 memories shown" in out
    assert out.rstrip().endswith("<!-- /ccmem -->")


def test_render_no_age_strings():
    from ccmem.generate import _render
    from ccmem.retrieval import Memory
    mems = [Memory("id1", "decision", "chose X", None, "project", "2026-01-01T00:00:00Z", 0)]
    out = _render(mems, 400)
    assert "[decision]" in out
    assert "- [decision | " not in out  # old "[type | age]" format is gone


def test_render_whole_file_within_cap():
    from ccmem.generate import _render, _estimate_tokens, GLOBAL_CAP
    from ccmem.retrieval import Memory
    big = [Memory(f"id{i:03d}", "decision", "x" * 80, None, "global",
                  f"2026-01-{(i % 28) + 1:02d}T00:00:00Z", 0) for i in range(60)]
    out = _render(big, GLOBAL_CAP)
    assert _estimate_tokens(out) <= GLOBAL_CAP  # WHOLE FILE incl header/footer/count


def test_render_no_partial_line():
    from ccmem.generate import _render, GLOBAL_CAP
    from ccmem.retrieval import Memory
    big = [Memory(f"id{i:03d}", "decision", f"memory number {i} " + "y" * 60, None,
                  "global", f"2026-01-{(i % 28) + 1:02d}T00:00:00Z", 0) for i in range(60)]
    out = _render(big, GLOBAL_CAP)
    for line in out.splitlines():
        if line.startswith("- ["):
            assert line.startswith("- [decision] memory number")


def test_render_recency_selection_then_created_asc_render():
    from ccmem.generate import _render
    from ccmem.retrieval import Memory
    mems_recency_desc = [
        Memory("c", "decision", "CCC", None, "global", "2026-01-03T00:00:00Z", 0),
        Memory("b", "decision", "BBB", None, "global", "2026-01-02T00:00:00Z", 0),
        Memory("a", "decision", "AAA", None, "global", "2026-01-01T00:00:00Z", 0),
    ]
    out = _render(mems_recency_desc, 30)
    assert "AAA" not in out             # oldest dropped at the cap
    assert "BBB" in out and "CCC" in out
    assert out.index("BBB") < out.index("CCC")  # render order is created_at ASC


def test_generate_global_excludes_project_scope():
    from ccmem.generate import generate_global
    import hashlib
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        pid = hashlib.sha256(b"/proj").hexdigest()[:16]
        _seed(con, [
            ("g1", "decision", "global fact", None, "global", pid, "/proj", "2026-01-01T00:00:00Z", "active", 0),
            ("p1", "decision", "project fact", None, "project", pid, "/proj", "2026-01-02T00:00:00Z", "active", 0),
        ])
        text = generate_global(con, claude_home=Path(tmp) / "claude").read_text(encoding="utf-8")
        assert "global fact" in text and "project fact" not in text
        con.close()


# --- Task 2: gitignore-first, atomic write, empty stub ---------------------


def test_generate_project_gitignore_is_self_contained():
    from ccmem.generate import generate_project
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        _seed(con, [])
        generate_project(con, tmp, require_gitignore=True)
        gi = Path(tmp) / ".ccmem" / ".gitignore"
        assert gi.exists()
        assert gi.read_text(encoding="utf-8").strip() == "*"
        assert not (Path(tmp) / ".gitignore").exists()  # repo .gitignore untouched
        con.close()


def test_generate_project_gitignore_idempotent():
    from ccmem.generate import generate_project
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        _seed(con, [])
        generate_project(con, tmp)
        generate_project(con, tmp)
        gi = Path(tmp) / ".ccmem" / ".gitignore"
        assert gi.read_text(encoding="utf-8").strip() == "*"
        con.close()


def test_generate_project_writes_memories_after_protection():
    from ccmem.generate import generate_project
    import hashlib
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        pid = hashlib.sha256(tmp.encode()).hexdigest()[:16]
        _seed(con, [
            ("p1", "gotcha", "port 8076 closed", None, "project", pid, tmp, "2026-01-01T00:00:00Z", "active", 0),
        ])
        path = generate_project(con, tmp)
        assert path.exists()
        assert "port 8076 closed" in path.read_text(encoding="utf-8")
        con.close()


def test_generate_project_no_memories_file_when_protection_fails():
    from ccmem.generate import generate_project
    import pytest
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        _seed(con, [])
        # Make .ccmem a FILE so the .gitignore write cannot succeed.
        ccmem_path = Path(tmp) / ".ccmem"
        ccmem_path.write_text("blocker", encoding="utf-8")
        with pytest.raises(RuntimeError):
            generate_project(con, tmp, require_gitignore=True)
        # .ccmem is still a file (not a dir), so no memories.md could have been written.
        assert ccmem_path.is_file()
        con.close()


def test_generate_project_empty_db_writes_stub():
    from ccmem.generate import generate_project
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        _seed(con, [])
        path = generate_project(con, tmp)
        text = path.read_text(encoding="utf-8")
        assert "<!-- ccmem -->" in text and "<!-- /ccmem -->" in text
        assert "0 of 0 memories shown" in text
        con.close()


def test_write_atomic_leaves_no_tmp():
    from ccmem.generate import _write_atomic
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.md"
        _write_atomic(path, "hello\n")
        assert path.read_text(encoding="utf-8") == "hello\n"
        assert list(Path(tmp).glob("*.tmp")) == []
