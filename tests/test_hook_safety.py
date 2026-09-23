"""Safety contract tests for all five hook scripts.

These tests are written BEFORE the hook implementations so they serve as
failing specs. Each proves a property that cannot be verified by claim:

  K  Kill-switch: CCMEM_DISABLED=1 → exit 0 in <50ms, empty stdout
  F  Failure silent: missing / corrupt / unusable DB → exit 0, empty stdout
  E  Exit-2 ban: no hook may exit 2 under any input (blocks/erases prompt)

Hooks not yet implemented will cause pytest.fail() with a clear message,
not a Python exception — so the failure is diagnostic, not confusing.

Run:
    pytest tests/test_hook_safety.py -v
"""

from __future__ import annotations

import json
import os
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
    "SessionStart":     {"session_id": "test-0", "transcript_path": "", "cwd": str(REPO), "hook_event_name": "SessionStart", "source": "startup"},
    "UserPromptSubmit": {"session_id": "test-0", "transcript_path": "", "cwd": str(REPO), "hook_event_name": "UserPromptSubmit", "prompt": "hello"},
    "Stop":             {"session_id": "test-0", "transcript_path": "", "cwd": str(REPO), "hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "done"},
    "SessionEnd":       {"session_id": "test-0", "transcript_path": "", "cwd": str(REPO), "hook_event_name": "SessionEnd", "reason": "exit"},
    "PreCompact":       {"session_id": "test-0", "transcript_path": "", "cwd": str(REPO), "hook_event_name": "PreCompact", "trigger": "auto", "custom_instructions": ""},
}

HOSTILE_INPUTS = [
    b"",                # empty stdin
    b"not json at all", # not JSON
    b'{"broken":}',     # malformed JSON
    b"null",            # JSON null
    b"[]",              # JSON array
    b"x" * 1_048_576,  # 1 MB of garbage
]

HOSTILE_IDS = ["empty", "not-json", "malformed-json", "null", "array", "1mb-garbage"]


def _require_hook(name: str) -> Path:
    path = HOOKS_DIR / name
    if not path.exists():
        pytest.fail(
            f"hooks/{name} does not exist — implement it before this test can pass. "
            "Phase 1 Task (see docs/PLAN.md)."
        )
    return path


def _default_timeout_s() -> float:
    """Derive test timeout from gate budget + 50% headroom (in seconds).

    Gate budget_ms values are observed p90 under active sync. The extra
    50% absorbs load amplification when the full suite runs concurrently.
    Falls back to 15s if config is missing.
    """
    import json as _json
    cfg_path = REPO / "gates" / "config.json"
    try:
        cfg = _json.loads(cfg_path.read_text())
        budgets = [v for v in cfg.get("budget_ms", {}).values() if isinstance(v, (int, float))]
        max_budget_ms = max(budgets) if budgets else 6000
        return (max_budget_ms * 1.5) / 1000
    except Exception:
        return 15.0


_TIMEOUT_S = _default_timeout_s()


def _run(
    hook_path: Path,
    payload: bytes,
    env_extra: dict | None = None,
    timeout: float = _TIMEOUT_S,
) -> tuple[subprocess.CompletedProcess, float]:
    env = os.environ.copy()
    # Default CCMEM_HOME — tests that need isolation override via env_extra.
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
        cwd=str(REPO),  # Fix: hooks resolve paths relative to repo root
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return proc, elapsed_ms


def _stderr(proc: subprocess.CompletedProcess) -> str:
    """Decode stderr safely; non-UTF-8 bytes become replacement chars, not exceptions."""
    return proc.stderr.decode("utf-8", "replace")


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
        f"stderr: {_stderr(proc)[:200]}"
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


def _kill_switch_threshold_ms() -> float:
    """Read interpreter_floor_ms + kill_switch_headroom_ms from gates/config.json.

    Falls back to 2500ms if config is missing. The threshold is floor + headroom
    because a disabled hook still pays interpreter startup but must not do any
    real work (DB connect, embedding, network).
    """
    import json as _json
    cfg_path = REPO / "gates" / "config.json"
    try:
        cfg = _json.loads(cfg_path.read_text())
        return cfg["interpreter_floor_ms"] + cfg["kill_switch_headroom_ms"]
    except Exception:
        return 2500.0


@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_kill_switch_within_floor_ms(hook_name, event):
    """CCMEM_DISABLED=1 → hook exits within interpreter_floor + headroom ms.

    The threshold is floor(1700ms) + headroom(800ms) = 2500ms on this machine;
    values come from gates/config.json. A disabled hook still pays interpreter
    startup but must not do any real work (DB connect, retrieval, network I/O).
    """
    hook = _require_hook(hook_name)
    payload = json.dumps(_PAYLOADS[event]).encode()
    threshold = _kill_switch_threshold_ms()
    _, elapsed_ms = _run(hook, payload, env_extra={"CCMEM_DISABLED": "1"})
    assert elapsed_ms < threshold, (
        f"{hook_name}: kill-switch took {elapsed_ms:.0f}ms > {threshold:.0f}ms threshold — "
        "the disabled check must be the very first thing the hook does, before any I/O"
    )


# ---------------------------------------------------------------------------
# F: Failure silent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_absent_db_exits_zero(hook_name, event, tmp_path):
    """Missing DB → hook exits 0 (session starts normally, no memory)."""
    hook = _require_hook(hook_name)
    payload = json.dumps(_PAYLOADS[event]).encode()
    absent = tmp_path / "nonexistent"  # directory doesn't exist → mem.db absent
    proc, _ = _run(hook, payload, env_extra={"CCMEM_HOME": str(absent)})
    assert proc.returncode == 0, (
        f"{hook_name}: absent DB caused exit {proc.returncode}. "
        f"stderr: {_stderr(proc)[:200]}"
    )


@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_absent_db_empty_stdout(hook_name, event, tmp_path):
    """Missing DB → hook injects nothing (stdout empty or valid JSON with no context)."""
    hook = _require_hook(hook_name)
    payload = json.dumps(_PAYLOADS[event]).encode()
    absent = tmp_path / "nonexistent"
    proc, _ = _run(hook, payload, env_extra={"CCMEM_HOME": str(absent)})
    stdout = proc.stdout.strip()
    if stdout:
        try:
            obj = json.loads(stdout)
        except json.JSONDecodeError:
            pytest.fail(f"{hook_name}: absent DB produced non-JSON stdout: {stdout[:80]!r}")
        ctx = obj.get("hookSpecificOutput", {}).get("additionalContext", "")
        # Guard against JSON null: key present but value is null → None
        assert not (ctx or "").strip(), (
            f"{hook_name}: absent DB produced non-empty additionalContext: {(ctx or '')[:80]!r}"
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
        f"stderr: {_stderr(proc)[:200]}"
    )


@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_unusable_db_exits_zero(hook_name, event, tmp_path):
    """Unusable DB path (mem.db is a directory) → hook exits 0.

    Replacing the DB file with a directory of the same name forces
    sqlite3.connect() to fail — this exercises the silent-failure path in
    every hook, including read-only hooks that wouldn't be caught by a mere
    read-only permission bit (stat.S_IREAD leaves the file readable).
    Works cross-platform without requiring chmod or admin access.
    """
    hook = _require_hook(hook_name)
    payload = json.dumps(_PAYLOADS[event]).encode()
    db_dir = tmp_path / "dir_home"
    db_dir.mkdir()
    # Make mem.db a directory — sqlite3 cannot open a directory as a database file
    db_as_dir = db_dir / "mem.db"
    db_as_dir.mkdir()
    proc, _ = _run(hook, payload, env_extra={"CCMEM_HOME": str(db_dir)})
    assert proc.returncode == 0, (
        f"{hook_name}: unusable DB (is a directory) caused exit {proc.returncode}. "
        f"stderr: {_stderr(proc)[:200]}"
    )


# ---------------------------------------------------------------------------
# E: Exit-2 ban
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hostile_input", HOSTILE_INPUTS, ids=HOSTILE_IDS)
def test_mem_retrieve_never_exits_2(hostile_input, tmp_path):
    """mem_retrieve.py must exit 0 on any input, including malformed.

    Exit 2 on UserPromptSubmit is uniquely destructive: Claude Code
    interprets it as 'block this prompt' and discards the user's message.
    R1 requires exit 0 on any error — this is the strictest case of that rule.
    """
    hook = _require_hook("mem_retrieve.py")
    proc, _ = _run(hook, hostile_input, env_extra={"CCMEM_HOME": str(tmp_path)})
    assert proc.returncode == 0, (
        f"mem_retrieve.py exited {proc.returncode} on input {hostile_input[:40]!r} — "
        "exit 2 on UserPromptSubmit discards the user's prompt; "
        "any non-zero exit violates R1. The hook MUST exit 0 on any error."
    )


@pytest.mark.parametrize("hostile_input", HOSTILE_INPUTS, ids=HOSTILE_IDS)
@pytest.mark.parametrize("hook_name,event", ALL_HOOKS)
def test_hook_exits_zero_on_hostile_inputs(hook_name, event, hostile_input, tmp_path):
    """All hooks exit 0 (not 1, not 2) on every hostile input.

    Each (hook, input) pair is an independent test so the first failure
    does not hide whether remaining inputs also fail.
    """
    hook = _require_hook(hook_name)
    proc, _ = _run(hook, hostile_input, env_extra={"CCMEM_HOME": str(tmp_path)})
    assert proc.returncode == 0, (
        f"{hook_name}: exited {proc.returncode} on hostile input {hostile_input[:40]!r}. "
        f"stderr: {_stderr(proc)[:100]}"
    )
