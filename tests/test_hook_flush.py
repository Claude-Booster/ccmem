# tests/test_hook_flush.py
import json, os, subprocess, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "fixtures", "transcripts")

def test_sessionend_captures_and_dedups_after_precompact(tmp_path):
    home = str(tmp_path)
    from ccmem.db import connect, migrate
    migrate(connect(os.path.join(home, "mem.db")))
    env = {**os.environ, "CCMEM_HOME": home, "PYTHONPATH": REPO}
    args = dict(capture_output=True, env=env, timeout=30)
    pc = subprocess.run([sys.executable, os.path.join(REPO, "hooks", "mem_snapshot.py")],
        input=json.dumps({"hook_event_name":"PreCompact",
            "transcript_path": os.path.join(FIX,"basic.jsonl"),"session_id":"s1"}).encode(), **args)
    se = subprocess.run([sys.executable, os.path.join(REPO, "hooks", "mem_flush.py")],
        input=json.dumps({"hook_event_name":"SessionEnd",
            "transcript_path": os.path.join(FIX,"basic.jsonl"),"session_id":"s1"}).encode(), **args)
    assert pc.returncode == 0 and se.returncode == 0
    con = connect(os.path.join(home, "mem.db"))
    # cross-event idempotency: no duplicate from the second capture
    assert con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
