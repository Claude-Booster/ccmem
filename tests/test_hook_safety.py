"""Safety contract tests for all five hook scripts.

These tests are written BEFORE the hook implementations so they serve as
failing specs. Each proves a property that cannot be verified by claim:

  K  Kill-switch: CCMEM_DISABLED=1 → exit 0 in <50ms, empty stdout
  F  Failure silent: missing / corrupt / unreadable DB → exit 0, empty stdout
  E  Exit-2 ban: no hook may exit 2 under any input (blocks/erases prompt)

Hooks not yet implemented will cause pytest.fail() with a clear message,
not a Python exception — so the failure is diagnostic, not confusing.

Run:
    pytest tests/test_hook_safety.py -v
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
HOOKS_DIR = REPO / "hooks"

ALL_HOOKS = [
    ("mem_inject.py",   "SessionStart"),
    ("mem_retrieve.py", "UserPromptSubmit"),
    ("mem_capture.py",  "Stop"),
    ("mem_flush.py",    "SessionEnd"),
    ("mem_snapshot.py", "PreCompact"),
]

# Minimal payloads per event — same shape as gates/_common.py base_payload().
_PAYLOADS = {
    "SessionStart":      {"session_id": "test-0", "transcript_path": "", "cwd": str(REPO), "hook_event_name": "SessionStart", "source": "startup"},
    "UserPromptSubmit":  {"session_id": "test-0", "transcript_path": "", "cwd": str(REPO), "hook_event_name": "UserPromptSubmit", "prompt": "hello"},
    "Stop":              {"session_id": "test-0", "transcript_path": "", "cwd": str(REPO), "hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "done"},
    "SessionEnd":        {"session_id": "test-0", "transcript_path": "", "cwd": str(REPO), "hook_event_name": "SessionEnd", "reason": "exit"},
    "PreCompact":        {"session_id": "test-0", "transcript_path": "", "cwd": str(REPO), "hook_event_name": "PreCompact", "trigger": "auto", "custom_instructions": ""},
}

HOSTILE_INPUTS = [
    b"",                          # empty stdin
    b"not json at all",           # not JSON
    b'{"broken":}',               # malformed JSON
    b"null",                      # JSON null
    b"[]",                        # JSON array
    b"x" * 1_048_576,             # 1 MB of garbage
]


def _require_hook(name: str) -> Path:
    path = HOOKS_DIR / name
    if not path.exists():
        pytest.fail(
            f"hooks/{name} does not exist — implement it before this test can pass. "
            f"Phase 1 Task (see docs/PLAN.md)."
        )
    return path


def _run(hook_path: Path, payload: bytes, env_extra: dict | None = None, timeout: float = 5.0) -> tuple[subprocess.CompletedProcess, float]:
    env = os.environ.copy()
    env["CCMEM_HOME"] = str(REPO / ".ccmem-safety-test")
    if env_extra:
        env.update(env_extra)
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(hook_path)],
        input=payload,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return proc, elapsed_ms


# ---------------------------------------------------------------------------
# K: Kill-switch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_kill_switch_exits_zero(hook_name, event):
    """CCMEM_DISABLED=1 → hook exits 0."""
    hook = _require_hook(hook_name)
    payload = json.dumps(_PAYLOADS[event]).encode()
    proc, _ = _run(hook, payload, env_extra={"CCMEM_DISABLED": "1"})
    assert proc.returncode == 0, (
        f"{hook_name}: expected exit 0 with CCMEM_DISABLED=1, got {proc.returncode}. "
        f"stderr: {proc.stderr.decode()[:200]}"
    )


@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_kill_switch_empty_stdout(hook_name, event):
    """CCMEM_DISABLED=1 → hook writes nothing to stdout (injects nothing)."""
    hook = _require_hook(hook_name)
    payload = json.dumps(_PAYLOADS[event]).encode()
    proc, _ = _run(hook, payload, env_extra={"CCMEM_DISABLED": "1"})
    assert proc.stdout.strip() == b"", (
        f"{hook_name}: expected empty stdout with CCMEM_DISABLED=1, "
        f"got: {proc.stdout[:80]!r}"
    )


@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_kill_switch_within_50ms(hook_name, event):
    """CCMEM_DISABLED=1 → hook exits within 50ms (checked at process level)."""
    hook = _require_hook(hook_name)
    payload = json.dumps(_PAYLOADS[event]).encode()
    _, elapsed_ms = _run(hook, payload, env_extra={"CCMEM_DISABLED": "1"})
    # 50ms is the spec; we give 200ms headroom for process startup on slow CI
    assert elapsed_ms < 200, (
        f"{hook_name}: kill-switch took {elapsed_ms:.0f}ms — "
        "check that the disabled path is the very first thing the hook does"
    )


# ---------------------------------------------------------------------------
# F: Failure silent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_absent_db_exits_zero(hook_name, event, tmp_path):
    """Missing DB → hook exits 0 (session starts normally, no memory)."""
    hook = _require_hook(hook_name)
    payload = json.dumps(_PAYLOADS[event]).encode()
    absent = tmp_path / "nonexistent" / "mem.db"  # parent dir doesn't exist
    proc, _ = _run(hook, payload, env_extra={"CCMEM_HOME": str(absent.parent)})
    assert proc.returncode == 0, (
        f"{hook_name}: absent DB caused exit {proc.returncode}. "
        f"stderr: {proc.stderr.decode()[:200]}"
    )


@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_absent_db_empty_stdout(hook_name, event, tmp_path):
    """Missing DB → hook injects nothing (stdout empty or valid JSON with no context)."""
    hook = _require_hook(hook_name)
    payload = json.dumps(_PAYLOADS[event]).encode()
    absent = tmp_path / "nonexistent" / "mem.db"
    proc, _ = _run(hook, payload, env_extra={"CCMEM_HOME": str(absent.parent)})
    stdout = proc.stdout.strip()
    if stdout:
        # If there IS output, it must be valid JSON with empty additionalContext
        try:
            obj = json.loads(stdout)
        except json.JSONDecodeError:
            pytest.fail(f"{hook_name}: absent DB produced non-JSON stdout: {stdout[:80]!r}")
        ctx = obj.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert not ctx.strip(), (
            f"{hook_name}: absent DB produced non-empty additionalContext: {ctx[:80]!r}"
        )


@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_corrupt_db_exits_zero(hook_name, event, tmp_path):
    """Corrupt DB (non-SQLite content) → hook exits 0."""
    hook = _require_hook(hook_name)
    payload = json.dumps(_PAYLOADS[event]).encode()
    db_dir = tmp_path / "corrupt_home"
    db_dir.mkdir()
    db = db_dir / "mem.db"
    db.write_bytes(b"this is not a sqlite database \x00\xff" * 100)
    proc, _ = _run(hook, payload, env_extra={"CCMEM_HOME": str(db_dir)})
    assert proc.returncode == 0, (
        f"{hook_name}: corrupt DB caused exit {proc.returncode}. "
        f"stderr: {proc.stderr.decode()[:200]}"
    )


@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_readonly_db_exits_zero(hook_name, event, tmp_path):
    """Unreadable DB (permissions) → hook exits 0.

    On Windows, os.chmod with S_IREAD makes the file read-only. The hook
    attempts to open for writing and must fail silently.
    """
    hook = _require_hook(hook_name)
    payload = json.dumps(_PAYLOADS[event]).encode()
    db_dir = tmp_path / "locked_home"
    db_dir.mkdir()
    db = db_dir / "mem.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 96)  # valid header stub
    # Remove write permission (read-only on Windows, 000 on POSIX)
    db.chmod(stat.S_IREAD)
    try:
        proc, _ = _run(hook, payload, env_extra={"CCMEM_HOME": str(db_dir)})
        assert proc.returncode == 0, (
            f"{hook_name}: locked DB caused exit {proc.returncode}. "
            f"stderr: {proc.stderr.decode()[:200]}"
        )
    finally:
        # Restore so pytest can clean up tmp_path on Windows
        try:
            db.chmod(stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# E: Exit-2 ban (mem_retrieve.py only — it's the one that would erase the prompt)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hostile_input", HOSTILE_INPUTS, ids=[
    "empty", "not-json", "malformed-json", "null", "array", "1mb-garbage"
])
def test_mem_retrieve_never_exits_2(hostile_input):
    """mem_retrieve.py must not exit 2 under any input.

    Claude Code interprets exit 2 from UserPromptSubmit as 'block this prompt'
    and discards the user's message. This is the worst possible hook failure.
    """
    hook = _require_hook("mem_retrieve.py")
    proc, _ = _run(hook, hostile_input)
    assert proc.returncode != 2, (
        f"mem_retrieve.py exited 2 on input {hostile_input[:40]!r} — "
        "exit 2 on UserPromptSubmit discards the user's prompt. "
        "The hook MUST exit 0 on any error."
    )


@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_hook_exits_zero_on_hostile_inputs(hook_name, event):
    """All hooks exit 0 (not 1, not 2) on every hostile input."""
    hook = _require_hook(hook_name)
    for blob in HOSTILE_INPUTS:
        proc, _ = _run(hook, blob)
        assert proc.returncode == 0, (
            f"{hook_name}: exited {proc.returncode} on hostile input {blob[:40]!r}. "
            f"stderr: {proc.stderr.decode()[:100]}"
        )
