import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
HOOK = REPO / "hooks" / "mem_inject.py"
FIXTURES = REPO / "fixtures"


def run_hook(payload_file, env_extra=None, db_dir=None):
    payload = json.loads((FIXTURES / payload_file).read_text())
    payload["cwd"] = str(REPO)
    env = os.environ.copy()
    env["CCMEM_HOME"] = db_dir or str(REPO / ".ccmem-test")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=10,
        env=env,
    )


def test_exits_zero_when_db_absent(tmp_path):
    proc = run_hook("payload_session_start.json", db_dir=str(tmp_path / "nonexistent"))
    assert proc.returncode == 0


def test_exits_zero_on_kill_switch():
    proc = run_hook("payload_session_start.json", env_extra={"CCMEM_DISABLED": "1"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == b""


def test_no_output_when_db_empty(tmp_path):
    from ccmem.db import connect, migrate
    con = connect(tmp_path / "mem.db")
    migrate(con)
    con.close()
    proc = run_hook("payload_session_start.json", db_dir=str(tmp_path))
    assert proc.returncode == 0
    assert proc.stdout.strip() == b""


def test_injects_context_with_memories(tmp_path):
    from ccmem.db import connect, migrate
    from ccmem.scoping import project_key
    con = connect(tmp_path / "mem.db")
    migrate(con)
    pid, _ = project_key(str(REPO))
    con.execute("""
        INSERT INTO memories (id, type, content, subject, scope,
                              project_id, project_root, created_at, status)
        VALUES ('m1','decision','chose RLS triggers','rate-limiter','project',?,?,?,?)
    """, (pid, str(REPO), "2026-09-01T00:00:00Z", "active"))
    con.commit()
    con.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    con.commit()
    con.close()
    proc = run_hook("payload_session_start.json", db_dir=str(tmp_path))
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "<ccmem-memories>" in ctx
    assert "chose RLS triggers" in ctx


def test_output_is_valid_json_when_memories_present(tmp_path):
    from ccmem.db import connect, migrate
    from ccmem.scoping import project_key
    con = connect(tmp_path / "mem.db")
    migrate(con)
    pid, _ = project_key(str(REPO))
    con.execute("""
        INSERT INTO memories (id, type, content, subject, scope,
                              project_id, project_root, created_at, status)
        VALUES ('m1','decision','test memory','subj','project',?,?,'2026-01-01T00:00:00Z','active')
    """, (pid, str(REPO)))
    con.commit()
    con.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    con.commit()
    con.close()
    proc = run_hook("payload_session_start.json", db_dir=str(tmp_path))
    assert proc.returncode == 0
    obj = json.loads(proc.stdout)
    assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_survives_hostile_inputs():
    hostile = [b"", b"not json", b'{"broken":}', b"null", b"[]"]
    for blob in hostile:
        env = os.environ.copy()
        env["CCMEM_HOME"] = str(REPO / ".ccmem-test")
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=blob, capture_output=True, timeout=10, env=env,
        )
        assert proc.returncode == 0, f"failed on {blob!r}"
