import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
HOOK = REPO / "hooks" / "mem_capture.py"
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
        capture_output=True, timeout=5, env=env,
    )


def test_exits_zero():
    proc = run_hook("payload_stop.json")
    assert proc.returncode == 0


def test_kill_switch_silent():
    proc = run_hook("payload_stop.json", env_extra={"CCMEM_DISABLED": "1"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == b""


def test_emits_no_context():
    proc = run_hook("payload_stop.json")
    if proc.stdout.strip():
        obj = json.loads(proc.stdout)
        ctx = obj.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert not ctx.strip()


def test_enqueues_high_score_turn(tmp_path):
    from ccmem.db import connect, migrate
    con = connect(tmp_path / "mem.db")
    migrate(con)
    con.close()
    payload = json.loads((FIXTURES / "payload_stop.json").read_text())
    payload["last_assistant_message"] = "We decided to use RLS triggers from now on."
    payload["cwd"] = str(REPO)
    env = os.environ.copy()
    env["CCMEM_HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True, timeout=5, env=env,
    )
    assert proc.returncode == 0
    from ccmem.db import connect as c2
    con2 = c2(tmp_path / "mem.db")
    rows = con2.execute("SELECT * FROM candidates").fetchall()
    con2.close()
    assert len(rows) >= 1


def test_survives_hostile_inputs():
    for blob in [b"", b"not json", b"null"]:
        env = os.environ.copy()
        env["CCMEM_HOME"] = str(REPO / ".ccmem-test")
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=blob, capture_output=True, timeout=5, env=env,
        )
        assert proc.returncode == 0
