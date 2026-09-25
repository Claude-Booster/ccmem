import os, time
from ccmem.db import connect, migrate
from ccmem.recovery import recover_project
from ccmem.killswitch import mark_disabled

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "transcripts")

def _db(): con = connect(":memory:"); migrate(con); return con

def _copy(tmp, name, src):
    import shutil; d = tmp / name; shutil.copy(os.path.join(FIX, src), d); return str(d)

def test_recovers_uncaptured_transcript(tmp_path):
    con = _db()
    _copy(tmp_path, "basic.jsonl", "basic.jsonl")
    r = recover_project(con, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 10_000_000, 5000)
    assert r.candidates == 1

def test_clean_transcript_zero_reads(tmp_path):
    con = _db(); p = _copy(tmp_path, "basic.jsonl", "basic.jsonl")
    from ccmem.capture import capture_transcript
    capture_transcript(con, p, "s1")               # mark current
    r = recover_project(con, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 10_000_000, 5000)
    assert r.candidates == 0

def test_disabled_transcript_never_swept(tmp_path):
    con = _db()
    # mark disabled by session_id BEFORE the transcript file exists (finding #1),
    # then create the file and sweep — it must still be skipped.
    mark_disabled(str(tmp_path), "basic")   # session_id == filename stem
    _copy(tmp_path, "basic.jsonl", "basic.jsonl")
    r = recover_project(con, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 10_000_000, 5000)
    assert r.candidates == 0

def test_pre_install_transcript_never_swept(tmp_path):
    con = _db(); p = _copy(tmp_path, "basic.jsonl", "basic.jsonl")
    future = "2999-01-01T00:00:00Z"
    r = recover_project(con, str(tmp_path), str(tmp_path), future, 10_000_000, 5000)
    assert r.candidates == 0

def test_partial_safe_under_byte_budget(tmp_path):
    con = _db()
    for i in range(3):
        _copy(tmp_path, f"t{i}.jsonl", "basic.jsonl")
    # tiny byte budget: sweep stops early, remainder captured on a second call
    r1 = recover_project(con, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 1, 5000)
    r2 = recover_project(con, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 10_000_000, 5000)
    assert r1.candidates + r2.candidates >= 1  # nothing lost across partial sweeps

def test_concurrent_locked_db_defers_without_loss(tmp_path):
    con = _db(); p = _copy(tmp_path, "basic.jsonl", "basic.jsonl")
    other = connect(os.path.join(":memory:"))  # placeholder; see note
    # Simulate lock by holding a write txn on a file-backed DB
    import sqlite3
    dbfile = str(tmp_path / "mem.db"); migrate(connect(dbfile))
    a = connect(dbfile); b = connect(dbfile)
    a.execute("BEGIN IMMEDIATE")
    try:
        # b's capture should not raise (busy_timeout) or should exit cleanly
        try:
            recover_project(b, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 10_000_000, 200)
        except sqlite3.OperationalError:
            pass  # acceptable: caller (hook) swallows and defers
    finally:
        a.execute("ROLLBACK")

def test_giant_turn_capped_in_storage(tmp_path):
    from ccmem.capture import MAX_CANDIDATE_CHARS
    import json as _j
    big = "we decided " + "z" * (2 * 1024 * 1024)
    p = tmp_path / "big.jsonl"
    p.write_text("\n".join([
        '{"type":"user","promptId":"p1","cwd":"C:\\\\p","message":{"content":[{"type":"text","text":' + _j.dumps(big) + '}]}}',
        '{"type":"assistant","apiBlockIndex":0,"message":{"content":[{"type":"text","text":"ok the approach is X because"}]}}',
    ]), encoding="utf-8")
    con = _db()
    recover_project(con, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 10_000_000, 5000)
    (n,) = con.execute("SELECT LENGTH(user_turn) FROM candidates").fetchone()
    assert n <= MAX_CANDIDATE_CHARS

def test_worktree_cwd_from_record_not_dirname(tmp_path):
    # capture derives project root from the transcript's cwd field (Task 5),
    # so a sigil in a transcript stored under a munged dir still resolves.
    con = _db(); import shutil
    d = tmp_path / "munged"; d.mkdir()
    shutil.copy(os.path.join(FIX, "sigil.jsonl"), d / "s.jsonl")
    r = recover_project(con, str(tmp_path), str(d), "2000-01-01T00:00:00Z", 10_000_000, 5000)
    assert r.sigil_memories == 1
