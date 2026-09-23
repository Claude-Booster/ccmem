import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
HOOK = REPO / "hooks" / "mem_retrieve.py"
FIXTURES = REPO / "fixtures"


def run_hook(payload_file, env_extra=None, db_dir=None):
    payload = json.loads((FIXTURES / payload_file).read_text())
    payload["cwd"] = str(REPO)
    env = os.environ.copy()
    env["CCMEM_HOME"] = db_dir or str(REPO / ".ccmem-test")
    env.pop("CCMEM_PER_TURN", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True, timeout=10, env=env,
    )


def test_no_injection_by_default():
    proc = run_hook("payload_user_prompt_submit.json")
    assert proc.returncode == 0
    if proc.stdout.strip():
        obj = json.loads(proc.stdout)
        ctx = obj.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert not ctx.strip()


def test_kill_switch_silent():
    proc = run_hook("payload_user_prompt_submit.json", env_extra={"CCMEM_DISABLED": "1"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == b""


def test_never_exits_2():
    payload = json.loads((FIXTURES / "payload_user_prompt_submit.json").read_text())
    payload["prompt"] = "x" * 1_000_000
    env = os.environ.copy()
    env["CCMEM_HOME"] = str(REPO / ".ccmem-test")
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True, timeout=10, env=env,
    )
    assert proc.returncode != 2


def test_sigil_capture_writes_to_db(tmp_path):
    from ccmem.db import connect, migrate
    con = connect(tmp_path / "mem.db")
    migrate(con)
    con.close()
    proc = run_hook(
        "payload_user_prompt_submit_sigil.json",
        db_dir=str(tmp_path),
    )
    assert proc.returncode == 0
    from ccmem.db import connect as c2
    con2 = c2(tmp_path / "mem.db")
    rows = con2.execute("SELECT content FROM memories WHERE status='active'").fetchall()
    con2.close()
    assert any("RLS triggers" in r[0] for r in rows)


def test_survives_hostile_inputs():
    hostile = [b"", b"not json", b"null"]
    for blob in hostile:
        env = os.environ.copy()
        env["CCMEM_HOME"] = str(REPO / ".ccmem-test")
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=blob, capture_output=True, timeout=10, env=env,
        )
        assert proc.returncode == 0, f"failed on {blob!r}"
