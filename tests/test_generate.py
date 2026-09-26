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


def test_pinned_survives_truncation_over_newer_unpinned():
    """Rev-2 follow-up #1: pinned memories are selected first, so an old pinned
    memory survives while a newer unpinned one drops at the cap."""
    from ccmem.generate import generate_global
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        try:
            # One old memory, pinned; then many newer unpinned (strictly increasing
            # timestamps) that would fill the cap. All unpinned are newer than pinned.
            rows = [("old", "decision", "PINNED OLD FACT", None, "global", "p", "/x",
                     "2026-01-01T00:00:00Z", "active", 0)]
            for i in range(40):
                rows.append((f"n{i:02d}", "decision", f"newer unpinned filler memory {i} " + "z" * 40,
                             None, "global", "p", "/x", f"2026-03-01T00:{i:02d}:00Z", "active", 0))
            _seed(con, rows)
            con.execute("UPDATE memories SET pinned=1 WHERE id='old'")
            con.commit()
            text = generate_global(con, claude_home=Path(tmp) / "claude").read_text(encoding="utf-8")
            assert "PINNED OLD FACT" in text          # pinned survived despite being oldest
            assert "filler memory 0 " not in text     # oldest unpinned dropped at the cap
            import re
            m = re.search(r"(\d+) of (\d+) memories shown", text)
            assert m and int(m.group(1)) < int(m.group(2))  # truncation actually happened
        finally:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()


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


def _git_init(path):
    import subprocess
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True,
                   stdin=subprocess.DEVNULL)


def test_generate_project_git_repo_actually_ignores_memories():
    """Rev-2 follow-up #2: verify git's real behaviour, not just the .gitignore write."""
    import subprocess, shutil
    if shutil.which("git") is None:
        import pytest
        pytest.skip("git not available")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _git_init(tmp)
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        try:
            _seed(con, [])
            from ccmem.generate import generate_project
            generate_project(con, tmp, require_gitignore=True)
            check = subprocess.run(["git", "check-ignore", "-q", ".ccmem/memories.md"],
                                   cwd=tmp, capture_output=True, stdin=subprocess.DEVNULL)
            assert check.returncode == 0  # git actually ignores it
        finally:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()


def test_generate_project_hard_fails_if_memories_already_tracked():
    """Rev-2 follow-up #2: if memories.md is already in the git index, .gitignore
    won't untrack it — must be a hard failure with rm --cached guidance."""
    import subprocess, shutil, pytest
    if shutil.which("git") is None:
        pytest.skip("git not available")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _git_init(tmp)
        # Create and force-track .ccmem/memories.md before generate runs.
        ccmem_dir = Path(tmp) / ".ccmem"
        ccmem_dir.mkdir()
        (ccmem_dir / "memories.md").write_text("stale tracked content\n", encoding="utf-8")
        subprocess.run(["git", "add", "-f", ".ccmem/memories.md"], cwd=tmp,
                       capture_output=True, check=True, stdin=subprocess.DEVNULL)
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        try:
            _seed(con, [])
            from ccmem.generate import generate_project
            with pytest.raises(RuntimeError, match="rm --cached"):
                generate_project(con, tmp, require_gitignore=True)
        finally:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()


def test_write_atomic_leaves_no_tmp():
    from ccmem.generate import _write_atomic
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.md"
        _write_atomic(path, "hello\n")
        assert path.read_text(encoding="utf-8") == "hello\n"
        assert list(Path(tmp).glob("*.tmp")) == []
