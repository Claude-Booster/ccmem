"""R1 exit-0 discipline + hostile-input survival for the CLI entrypoints.

Ported from the retired gate_hook_contract: the capture/generate commands now do
the work hooks used to do (hooks are blocked by allowManagedHooksOnly). They must
survive garbage input without crashing the caller — a scheduled capture wrapper
that dies on a malformed transcript is worse than no capture.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).parent.parent

# Transcript payloads a capture run must survive. None should crash the process.
HOSTILE_TRANSCRIPTS = {
    "empty file": b"",
    "not json": b"this is not json at all\n",
    "truncated json": b'{"type":"user","message"\n',
    "null line": b"null\n",
    "array line": b"[]\n",
    "wrong types": b'{"type":42,"message":null}\n',
    "1MB blob": b"x" * (1024 * 1024) + b"\n",
}


def _run(args, env=None, cwd=None):
    base = {**os.environ, "PYTHONPATH": str(REPO)}
    if env:
        base.update(env)
    return subprocess.run(
        [sys.executable, "-m", "ccmem.cli", *args],
        capture_output=True, text=True, env=base, cwd=cwd, stdin=subprocess.DEVNULL,
    )


def test_capture_survives_hostile_transcripts():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        env = {"CCMEM_HOME": tmp}
        for label, blob in HOSTILE_TRANSCRIPTS.items():
            t = os.path.join(tmp, "t.jsonl")
            with open(t, "wb") as f:
                f.write(blob)
            r = _run(["capture", t, "--session-id", "hostile"], env=env, cwd=tmp)
            assert r.returncode == 0, f"{label}: rc={r.returncode} stderr={r.stderr[:300]}"


def test_capture_missing_file_exits_zero():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        env = {"CCMEM_HOME": tmp}
        r = _run(["capture", os.path.join(tmp, "nope.jsonl"), "--session-id", "x"],
                 env=env, cwd=tmp)
        assert r.returncode == 0, r.stderr


def test_generate_on_empty_db_exits_zero():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        env = {"CCMEM_HOME": tmp, "USERPROFILE": tmp, "HOME": tmp}
        r = _run(["generate", "--global-only"], env=env, cwd=tmp)
        assert r.returncode == 0, r.stderr
