import json, os, subprocess, sys
from pathlib import Path

REPO = Path(__file__).parent.parent
HOOK = REPO / "hooks" / "mem_snapshot.py"
FIXTURES = REPO / "fixtures"


def run_hook(payload_file, env_extra=None, db_dir=None):
    payload = json.loads((FIXTURES / payload_file).read_text())
    env = os.environ.copy()
    env["CCMEM_HOME"] = db_dir or str(REPO / ".ccmem-test")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True, timeout=10, env=env,
    )


def test_exits_zero():
    assert run_hook("payload_pre_compact.json").returncode == 0


def test_kill_switch():
    proc = run_hook("payload_pre_compact.json", env_extra={"CCMEM_DISABLED": "1"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == b""


def test_emits_no_context():
    proc = run_hook("payload_pre_compact.json")
    if proc.stdout.strip():
        obj = json.loads(proc.stdout)
        assert not obj.get("hookSpecificOutput", {}).get("additionalContext", "").strip()


def test_survives_hostile_inputs():
    for blob in [b"", b"not json", b"null"]:
        env = os.environ.copy()
        env["CCMEM_HOME"] = str(REPO / ".ccmem-test")
        proc = subprocess.run([sys.executable, str(HOOK)],
            input=blob, capture_output=True, timeout=5, env=env)
        assert proc.returncode == 0
