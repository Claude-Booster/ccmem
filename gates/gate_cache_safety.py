#!/usr/bin/env python3
"""Gate: prompt-cache safety (CLAUDE.md R4, R5, R6).

Injected text that changes between otherwise-identical turns invalidates the
prompt cache from that point forward. On a long session that is real money --
there is a documented case of roughly $45 burned by a hook emitting a differing
success string (anthropics/claude-code#29963).

This gate proves determinism empirically rather than trusting a code reading.

Run: python gates/gate_cache_safety.py
"""

from __future__ import annotations

import json
import sys

from _common import (
    REPO_ROOT,
    GateResult,
    base_payload,
    ensure_seeded_db,
    hook_path,
    injected_text,
    load_config,
    run_hook,
    scan_for_volatility,
    source_files,
)

REPEATS = 3

# Places a PreToolUse/PostToolUse hook could be registered.
SETTINGS_CANDIDATES = [
    ".claude/settings.json",
    ".claude/settings.local.json",
    "plugin/hooks/hooks.json",
    "plugin/hooks.json",
]

TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch"}


def check_determinism(
    cfg: dict, r: GateResult, event: str, env_extra=None, require_output: bool = True
) -> None:
    script = hook_path(cfg, event)
    if not script.exists():
        r.fail(f"{event}: deterministic", f"missing {cfg['hooks'][event]}")
        return

    payload = base_payload(event)
    outputs = []
    for _ in range(REPEATS):
        run = run_hook(cfg, event, payload, env_extra=env_extra)
        if run.timed_out or run.returncode != 0:
            r.fail(f"{event}: deterministic", f"hook failed (rc={run.returncode})")
            return
        outputs.append(injected_text(run.stdout))

    # A determinism check that passes on empty output proves nothing. Seeding
    # happens before this runs, so empty here means retrieval is broken.
    if require_output and not outputs[0].strip():
        r.fail(
            f"{event}: byte-stable across {REPEATS} runs",
            "no injected output against a seeded DB -- determinism would be vacuous",
        )
        return

    if len(set(outputs)) == 1:
        r.ok(f"{event}: byte-stable across {REPEATS} runs", f"{len(outputs[0])} chars")
        return

    # Show the first divergence so the failure is actionable.
    a, b = outputs[0], next(o for o in outputs[1:] if o != outputs[0])
    idx = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    lo, hi = max(0, idx - 40), idx + 40
    r.fail(
        f"{event}: byte-stable across {REPEATS} runs",
        f"diverges at char {idx}: {a[lo:hi]!r} vs {b[lo:hi]!r}",
    )


def check_tool_hooks(r: GateResult) -> None:
    """R6: tool-level hooks must never return additionalContext."""
    offenders = []
    checked = []
    for rel in SETTINGS_CANDIDATES:
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        checked.append(rel)
        try:
            conf = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            r.fail(f"{rel}: parses", str(exc))
            continue
        hooks = conf.get("hooks", conf)
        if not isinstance(hooks, dict):
            continue
        for event in hooks:
            if event in TOOL_EVENTS:
                offenders.append(f"{rel}:{event}")

    if offenders:
        r.fail(
            "no ccmem hooks on tool events",
            f"{', '.join(offenders)} -- these re-serialise between turns and bust cache",
        )
    elif checked:
        r.ok("no ccmem hooks on tool events", ", ".join(checked))
    else:
        r.fail(
            "hook config exists",
            "no settings.json or hooks.json found -- nothing to validate",
        )


def check_per_turn_default_off(cfg: dict, r: GateResult) -> None:
    """R5: UserPromptSubmit injection is opt-in."""
    script = hook_path(cfg, "UserPromptSubmit")
    if not script.exists():
        r.fail("per-turn injection defaults off", "UserPromptSubmit hook missing")
        return

    run = run_hook(
        cfg,
        "UserPromptSubmit",
        base_payload("UserPromptSubmit"),
        env_extra={"CCMEM_PER_TURN": ""},
    )
    if injected_text(run.stdout).strip():
        r.fail(
            "per-turn injection defaults off",
            "injected context without CCMEM_PER_TURN=1",
        )
    else:
        r.ok("per-turn injection defaults off")

    # When explicitly enabled it must still be byte-stable. Empty output is
    # legitimate here -- retrieval may fall below the relevance floor.
    check_determinism(
        cfg, r, "UserPromptSubmit",
        env_extra={"CCMEM_PER_TURN": "1"}, require_output=False,
    )


def check_static_volatility(r: GateResult) -> None:
    """Belt and braces: flag volatile calls on the injection path.

    Annotate a deliberate use with a trailing `# ccmem: cache-safe` comment when
    the value provably does not reach injected output.
    """
    paths = source_files("hooks", "ccmem/render.py")
    if not paths:
        r.fail("injection path scanned", "no hooks/ or ccmem/render.py found")
        return
    hits: list[str] = []
    for path in paths:
        hits.extend(scan_for_volatility(path))
    if hits:
        r.fail(
            "no volatile values on injection path",
            "; ".join(hits[:6]) + (f" (+{len(hits) - 6} more)" if len(hits) > 6 else ""),
        )
    else:
        r.ok("no volatile values on injection path", f"{len(paths)} files scanned")


def check_cross_session_determinism(cfg: dict, r: GateResult) -> None:
    """Cross-session invariant: mark_accessed mutations must not change next session's output.

    The retrieval query sorts by access_count DESC. If mark_accessed fires BEFORE render
    in session N, the DB is already mutated when session N+1 retrieves. This check seeds
    fresh memories with non-uniform access_counts so that a top-K shuffle IS detectable,
    then asserts byte-identical output across two SessionStart runs.
    """
    import hashlib
    import os as _os
    import sqlite3
    import tempfile

    project_id = hashlib.sha256(str(REPO_ROOT).encode()).hexdigest()[:16]

    with tempfile.TemporaryDirectory() as tmp:
        db_path = _os.path.join(tmp, "mem.db")
        sys.path.insert(0, str(REPO_ROOT))
        try:
            from ccmem.db import connect, migrate  # type: ignore
        except Exception as exc:
            r.fail("cross-session: ccmem importable", str(exc))
            return

        con = connect(db_path)
        migrate(con)
        # Insert 15 memories with distinct access_counts so that a single
        # mark_accessed pass on the top-12 can reshuffle borderline items.
        for i in range(15):
            con.execute(
                "INSERT INTO memories "
                "(id, type, content, subject, scope, project_id, project_root, "
                "created_at, status, access_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"cs-test-{i:02d}",
                    "decision",
                    f"Cross-session fact {i}: chose approach {i % 3}.",
                    f"cs-subject-{i}",
                    "project",
                    project_id,
                    str(REPO_ROOT),
                    f"2026-01-{i+1:02d}T00:00:00Z",
                    "active",
                    i,  # access_count = 0..14; items 12-14 rank highest initially
                ),
            )
        con.commit()
        con.close()

        env_extra = {"CCMEM_HOME": tmp}
        payload = base_payload("SessionStart")

        run1 = run_hook(cfg, "SessionStart", payload, env_extra=env_extra)
        if run1.returncode != 0 or run1.timed_out:
            r.fail("cross-session: SessionStart stable across sessions",
                   f"run1 failed (rc={run1.returncode})")
            return
        out1 = injected_text(run1.stdout)
        if not out1.strip():
            r.fail("cross-session: SessionStart stable across sessions",
                   "no output in run1 -- check seeding or retrieval")
            return

        run2 = run_hook(cfg, "SessionStart", payload, env_extra=env_extra)
        if run2.returncode != 0 or run2.timed_out:
            r.fail("cross-session: SessionStart stable across sessions",
                   f"run2 failed (rc={run2.returncode})")
            return
        out2 = injected_text(run2.stdout)

        if out1 == out2:
            r.ok("cross-session: SessionStart stable across sessions",
                 f"{len(out1)} chars byte-identical after mark_accessed")
        else:
            idx = next(
                (i for i, (x, y) in enumerate(zip(out1, out2)) if x != y),
                min(len(out1), len(out2)),
            )
            lo, hi = max(0, idx - 40), idx + 40
            r.fail(
                "cross-session: SessionStart stable across sessions",
                f"diverges at char {idx}: {out1[lo:hi]!r} vs {out2[lo:hi]!r}",
            )


def main() -> int:
    cfg = load_config()
    r = GateResult("cache safety")

    db, err = ensure_seeded_db()
    if db is None:
        r.fail("test DB seeded", err)
    else:
        r.ok("test DB seeded", str(db.relative_to(REPO_ROOT)))

    check_determinism(cfg, r, "SessionStart")
    check_cross_session_determinism(cfg, r)
    check_per_turn_default_off(cfg, r)
    check_tool_hooks(r)
    check_static_volatility(r)

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
