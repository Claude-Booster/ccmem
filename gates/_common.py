"""Shared helpers for ccmem gates.

Gates are executable spec. They are allowed to be strict and they are allowed to
fail loudly. What they are not allowed to do is pass because the thing they check
does not exist yet -- a missing implementation is a FAIL, not a SKIP.

Stdlib only, on purpose. A gate that needs `pip install` is a gate that stops
getting run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

GATES_DIR = Path(__file__).resolve().parent
REPO_ROOT = GATES_DIR.parent

# Throwaway test home for gates. Kept OUT of the repo tree so a gate run never
# leaves a .ccmem-test/ that trips gate_scaffold's "absent at rest" check.
TEST_HOME = Path(tempfile.gettempdir()) / "ccmem-gate-test"

# Events whose stdout Claude Code injects into the model's context.
# See docs/FACTS.md §1 -- re-verify before trusting.
INJECTING_EVENTS = {"SessionStart", "UserPromptSubmit"}

DEFAULT_CONFIG = {
    "hooks": {
        "SessionStart": "hooks/mem_inject.py",
        "SessionEnd": "hooks/mem_flush.py",
        "PreCompact": "hooks/mem_snapshot.py",
    },
    # Wall-clock budget per event, milliseconds. Deliberately tighter than the
    # Claude Code timeouts -- we want headroom, not a photo finish.
    "budget_ms": {
        "SessionStart": 2000,
        "SessionEnd": 1200,
        "PreCompact": 2000,
    },
    "max_injected_chars": 10000,
    "max_injected_tokens": 1200,
    "topk_cap": 12,
    "injection_marker": "<ccmem-memories>",
}


def load_config() -> dict:
    path = GATES_DIR / "config.json"
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        user = json.loads(path.read_text(encoding="utf-8"))
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


# --------------------------------------------------------------------------
# Result plumbing
# --------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class GateResult:
    gate: str
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    def ok(self, name: str, detail: str = "") -> None:
        self.add(name, True, detail)

    def fail(self, name: str, detail: str) -> None:
        self.add(name, False, detail)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)

    def report(self) -> int:
        width = max((len(c.name) for c in self.checks), default=0)
        print(f"\n=== {self.gate} ===")
        for c in self.checks:
            mark = "PASS" if c.ok else "FAIL"
            print(f"  [{mark}] {c.name.ljust(width)}  {c.detail}".rstrip())
        if not self.checks:
            print("  [FAIL] gate ran no checks -- that is itself a failure")
            return 1
        return 0 if self.passed else 1


# --------------------------------------------------------------------------
# Hook invocation
# --------------------------------------------------------------------------


@dataclass
class HookRun:
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: float
    timed_out: bool = False


def hook_path(cfg: dict, event: str) -> Path:
    return REPO_ROOT / cfg["hooks"][event]


def run_hook(
    cfg: dict,
    event: str,
    payload: dict | str | bytes,
    env_extra: dict | None = None,
    timeout_s: float = 30.0,
) -> HookRun:
    """Drive a hook script the way Claude Code does: JSON on stdin, read stdout."""
    script = hook_path(cfg, event)
    if isinstance(payload, dict):
        data = json.dumps(payload).encode()
    elif isinstance(payload, str):
        data = payload.encode()
    else:
        data = payload

    env = os.environ.copy()
    env.setdefault("CLAUDE_PROJECT_DIR", str(REPO_ROOT))
    env.setdefault("CCMEM_HOME", str(TEST_HOME))
    if env_extra:
        env.update(env_extra)

    cmd = [sys.executable, str(script)] if script.suffix == ".py" else [str(script)]

    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            input=data,
            capture_output=True,
            timeout=timeout_s,
            env=env,
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired:
        return HookRun(-1, "", "timeout", (time.perf_counter() - start) * 1000, True)
    elapsed = (time.perf_counter() - start) * 1000
    return HookRun(
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
        elapsed,
    )


def parse_hook_output(stdout: str) -> dict | None:
    """Hook stdout is either plain text or a JSON control object. Return the JSON."""
    text = stdout.strip()
    if not text.startswith("{"):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def injected_text(stdout: str) -> str:
    """Whatever this hook would put in front of the model."""
    obj = parse_hook_output(stdout)
    if obj is None:
        return stdout
    hso = obj.get("hookSpecificOutput") or {}
    return hso.get("additionalContext", "") or ""


# --------------------------------------------------------------------------
# Payload fixtures
# --------------------------------------------------------------------------


def base_payload(event: str) -> dict:
    """A payload shaped like what Claude Code sends. Deliberately minimal.

    If docs/FACTS.md §2 changes, change this -- it is the contract under test.
    """
    common = {
        "session_id": "0000-test-session",
        "transcript_path": str(REPO_ROOT / "fixtures" / "transcript.jsonl"),
        "cwd": str(REPO_ROOT),
        "hook_event_name": event,
    }
    extra = {
        "SessionStart": {"source": "startup", "model": "claude-opus-5"},
        "UserPromptSubmit": {"prompt": "what did we decide about the rate limiter?"},
        "Stop": {"stop_hook_active": False, "last_assistant_message": "Done."},
        "SessionEnd": {"reason": "prompt_input_exit"},
        "PreCompact": {"trigger": "auto", "custom_instructions": ""},
    }
    return {**common, **extra.get(event, {})}


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------


def ensure_seeded_db(rows: int = 400) -> tuple[Path | None, str]:
    """Create and populate the throwaway test DB.

    Shared by the budget and cache-safety gates: a determinism check that runs
    against an empty database proves nothing, so both gates seed first.

    Schema matches DESIGN.md (content/context split, project_id/project_root, type,
    subject). Seed data exercises supersession: most rows have unique subjects, but a
    few share a subject to confirm the exact-subject supersession path is exercised by
    real data.

    Returns (db_path, error). db_path is None if ccmem isn't importable yet.
    """
    import hashlib

    sys.path.insert(0, str(REPO_ROOT))
    home = TEST_HOME
    home.mkdir(parents=True, exist_ok=True)
    db = home / "mem.db"

    try:
        from ccmem.db import connect, migrate  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return None, f"ccmem.db not importable: {type(exc).__name__}: {exc}"

    # Deliberate same-subject collisions: every 50th row uses "rate-limiter",
    # every 75th uses "database-pool". All others get a unique subject.
    COLLISION_SUBJECTS = {50: "rate-limiter", 75: "database-pool"}
    project_id = hashlib.sha256(str(REPO_ROOT).encode()).hexdigest()[:16]
    types = ["decision", "preference", "correction", "project_state", "gotcha"]

    try:
        con = connect(db)
        migrate(con)
        existing = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        for i in range(existing, rows):
            collision_key = next((k for k in COLLISION_SUBJECTS if i % k == 0 and i > 0), None)
            subject = COLLISION_SUBJECTS[collision_key] if collision_key else f"subsystem-{i}"
            con.execute(
                "INSERT OR IGNORE INTO memories "
                "(id, type, content, context, subject, scope, project_id, project_root, "
                "created_at, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"seed-{i:04d}",
                    types[i % len(types)],
                    # content: the injected fact — short, distinct from context
                    f"Seed fact {i}: chose approach {i % 7} for subsystem {i % 13}.",
                    # context: the reasoning — longer, intentionally different text
                    f"Reasoning for seed {i}: evaluated {i % 5} alternatives; "
                    f"latency under contention favored approach {i % 7} over "
                    f"option {(i + 1) % 7}.",
                    subject,
                    "project",
                    project_id,
                    str(REPO_ROOT),
                    "2026-01-01T00:00:00Z",
                    "active",
                ),
            )
        con.commit()
        con.close()
    except Exception as exc:  # noqa: BLE001
        return None, f"seeding failed: {type(exc).__name__}: {exc}"

    return db, ""


def estimate_tokens(text: str) -> int:
    """Crude chars/4 heuristic. Good enough for a budget ceiling, not for billing.

    Replace with a real tokenizer if one is already a dependency; do not add one
    just for this.
    """
    return (len(text) + 3) // 4


def source_files(*relative_dirs: str, suffix: str = ".py") -> list[Path]:
    found: list[Path] = []
    for rel in relative_dirs:
        root = REPO_ROOT / rel
        if root.is_file() and root.suffix == suffix:
            found.append(root)
        elif root.is_dir():
            found.extend(sorted(p for p in root.rglob(f"*{suffix}") if p.is_file()))
    return found


VOLATILE_PATTERNS = [
    (r"\bdatetime\.(now|utcnow|today)\s*\(", "wall-clock time"),
    (r"\btime\.(time|monotonic|perf_counter)\s*\(", "wall-clock time"),
    (r"\buuid\.uuid[14]\s*\(", "random uuid"),
    (r"\brandom\.", "randomness"),
    (r"\bos\.urandom\s*\(", "randomness"),
    (r"\bid\s*\(\s*\w+\s*\)", "object identity"),
]


def scan_for_volatility(path: Path) -> list[str]:
    """Heuristic: things that make output differ between identical invocations."""
    hits: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hits
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or "ccmem: cache-safe" in line:
            continue
        for pattern, label in VOLATILE_PATTERNS:
            if re.search(pattern, line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{line_no} {label}")
                break
    return hits
