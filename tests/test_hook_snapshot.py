# tests/test_hook_snapshot.py
import json, os, subprocess, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "fixtures", "transcripts")

def _run(payload, env):
    return subprocess.run(
        [sys.executable, os.path.join(REPO, "hooks", "mem_snapshot.py")],
        input=json.dumps(payload).encode(), capture_output=True, env=env, timeout=30)

def test_precompact_captures_and_marks(tmp_path):
    home = str(tmp_path)
    from ccmem.db import connect, migrate
    migrate(connect(os.path.join(home, "mem.db")))
    env = {**os.environ, "CCMEM_HOME": home, "PYTHONPATH": REPO}
    p = _run({"hook_event_name": "PreCompact",
              "transcript_path": os.path.join(FIX, "basic.jsonl"),
              "session_id": "s1"}, env)
    assert p.returncode == 0
    con = connect(os.path.join(home, "mem.db"))
    rows = con.execute("SELECT is_pre_compact FROM candidates").fetchall()
    assert rows and all(r[0] == 1 for r in rows)


def test_precompact_migrates_pre_v2_db(tmp_path):
    home = str(tmp_path)
    from ccmem.db import connect, migrate
    dbp = os.path.join(home, "mem.db")
    con = connect(dbp)
    migrate(con)
    # Simulate a pre-v2 DB: drop the v2-only table that capture depends on
    con.execute("DROP TABLE transcript_progress")
    con.commit()
    con.close()
    env = {**os.environ, "CCMEM_HOME": home, "PYTHONPATH": REPO}
    p = subprocess.run(
        [sys.executable, os.path.join(REPO, "hooks", "mem_snapshot.py")],
        input=json.dumps({
            "hook_event_name": "PreCompact",
            "transcript_path": os.path.join(FIX, "basic.jsonl"),
            "session_id": "s1",
        }).encode(),
        capture_output=True, env=env, timeout=30,
    )
    assert p.returncode == 0
    con = connect(dbp)
    # Hook must have re-created transcript_progress (migrate ran)
    assert con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transcript_progress'"
    ).fetchone()
    # And must have captured at least one candidate
    assert con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] >= 1
