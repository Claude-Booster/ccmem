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


def check_sessionstart_determinism(cfg: dict, r: GateResult) -> None:
    """SessionStart determinism: steady-state + at-most-once sweep-induced change.

    The recovery sweep now writes to the store (sigil memories, candidates, refusal
    notices) inside SessionStart. Byte-identical output across two runs is therefore
    only guaranteed once the sweep has nothing new to process.

    Two sub-checks:
    1. Steady state: with no dirty transcripts in the project dir, two consecutive
       SessionStart runs produce byte-identical injected output.
    2. At-most-once: with one dirty sigil transcript present, run 1 sweeps and may
       change the store; run 2 (no new dirt) must be byte-identical to run 3.
       Run 1 is allowed to differ from run 2 — this is the "one cache write on
       crash recovery" cost documented in the design spec.
    """
    import hashlib
    import os as _os
    import tempfile

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from ccmem.db import connect, migrate  # type: ignore
    except Exception as exc:
        r.fail("SessionStart: ccmem importable", str(exc))
        return

    project_id = hashlib.sha256(str(REPO_ROOT).encode()).hexdigest()[:16]

    def _seed(tmp: str, n: int = 10) -> None:
        """Insert n memories for REPO_ROOT's project so injection is non-empty.

        Also back-dates initialized_at so the sweep's mtime prefilter does NOT
        skip a transcript written after _seed() returns. (The prefilter skips any
        transcript whose mtime <= initialized_at; we want NOW > initialized_at.)
        """
        db_path = _os.path.join(tmp, "mem.db")
        con = connect(db_path)
        migrate(con)
        # Back-date initialized_at so transcripts written after _seed() (mtime=NOW)
        # are newer than the DB's install time and pass the sweep prefilter.
        con.execute(
            "UPDATE schema_meta SET value=? WHERE key='initialized_at'",
            ("2026-01-01T00:00:00Z",),
        )
        for i in range(n):
            con.execute(
                "INSERT OR IGNORE INTO memories "
                "(id, type, content, subject, scope, project_id, project_root, "
                "created_at, status, access_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"ss-seed-{i:02d}",
                    "decision",
                    f"Determinism seed {i}: chose approach {i % 3}.",
                    f"ss-subject-{i}",
                    "project",
                    project_id,
                    str(REPO_ROOT),
                    f"2026-01-{i + 1:02d}T00:00:00Z",
                    "active",
                    i,
                ),
            )
        con.commit()
        con.close()

    def _payload(proj_dir: str) -> dict:
        return {
            **base_payload("SessionStart"),
            "transcript_path": _os.path.join(proj_dir, "session.jsonl"),
            "cwd": str(REPO_ROOT),
        }

    # -- Sub-check 1: steady state (no dirty transcripts) -----------------------
    with tempfile.TemporaryDirectory() as tmp_ss:
        _seed(tmp_ss)
        proj_ss = _os.path.join(tmp_ss, "project")
        _os.makedirs(proj_ss, exist_ok=True)
        # proj_ss is empty: no *.jsonl → nothing for the sweep to process.

        env = {"CCMEM_HOME": tmp_ss}
        payload = _payload(proj_ss)

        r1 = run_hook(cfg, "SessionStart", payload, env_extra=env)
        if r1.returncode != 0 or r1.timed_out:
            r.fail("SessionStart: steady-state determinism",
                   f"run1 failed (rc={r1.returncode})")
        else:
            out1 = injected_text(r1.stdout)
            if not out1.strip():
                r.fail("SessionStart: steady-state determinism",
                       "no injected output -- seeding or retrieval broken")
            else:
                r2 = run_hook(cfg, "SessionStart", payload, env_extra=env)
                if r2.returncode != 0 or r2.timed_out:
                    r.fail("SessionStart: steady-state determinism",
                           f"run2 failed (rc={r2.returncode})")
                else:
                    out2 = injected_text(r2.stdout)
                    if out1 == out2:
                        r.ok("SessionStart: steady-state determinism",
                             f"{len(out1)} chars byte-identical")
                    else:
                        idx = next(
                            (i for i, (x, y) in enumerate(zip(out1, out2)) if x != y),
                            min(len(out1), len(out2)),
                        )
                        lo, hi = max(0, idx - 40), idx + 40
                        r.fail("SessionStart: steady-state determinism",
                               f"diverges at char {idx}: "
                               f"{out1[lo:hi]!r} vs {out2[lo:hi]!r}")

    # -- Sub-check 2: at-most-once sweep change ---------------------------------
    with tempfile.TemporaryDirectory() as tmp_amo:
        _seed(tmp_amo)
        proj_amo = _os.path.join(tmp_amo, "project")
        _os.makedirs(proj_amo, exist_ok=True)

        # Write a sigil transcript whose cwd matches REPO_ROOT so the captured
        # sigil memory belongs to the same project being retrieved, exercising
        # the genuine "run1 injects a new memory the sweep just wrote" path.
        sigil_transcript = (
            '{"type":"user","promptId":"p-amo-1","cwd":'
            + json.dumps(str(REPO_ROOT))
            + ',"message":{"content":[{"type":"text",'
            '"text":"!mem: at-most-once gate test memory"}]}}\n'
            '{"type":"assistant","apiBlockIndex":0,'
            '"message":{"content":[{"type":"text","text":"noted"}]}}\n'
        )
        sweep_target = _os.path.join(proj_amo, "sweep-test.jsonl")
        with open(sweep_target, "w", encoding="utf-8") as fh:
            fh.write(sigil_transcript)
        # mtime is NOW (just written) > initialized_at (fresh DB), so the sweep
        # will find and process it on the first run.

        env = {"CCMEM_HOME": tmp_amo}
        payload = _payload(proj_amo)

        r1 = run_hook(cfg, "SessionStart", payload, env_extra=env)
        if r1.returncode != 0 or r1.timed_out:
            r.fail("SessionStart: at-most-once sweep change",
                   f"run1 failed (rc={r1.returncode})")
            return
        out_r1 = injected_text(r1.stdout)
        if not out_r1.strip():
            r.fail("SessionStart: at-most-once sweep change",
                   "no injected output in run1 -- seeding or retrieval broken")
            return

        r2 = run_hook(cfg, "SessionStart", payload, env_extra=env)
        if r2.returncode != 0 or r2.timed_out:
            r.fail("SessionStart: at-most-once sweep change",
                   f"run2 failed (rc={r2.returncode})")
            return
        out_r2 = injected_text(r2.stdout)

        r3 = run_hook(cfg, "SessionStart", payload, env_extra=env)
        if r3.returncode != 0 or r3.timed_out:
            r.fail("SessionStart: at-most-once sweep change",
                   f"run3 failed (rc={r3.returncode})")
            return
        out_r3 = injected_text(r3.stdout)

        if out_r2 == out_r3:
            sweep_tag = "(run1 changed)" if out_r1 != out_r2 else "(run1 stable)"
            r.ok("SessionStart: at-most-once sweep change",
                 f"run2==run3 ({len(out_r2)} chars) {sweep_tag}")
        else:
            idx = next(
                (i for i, (x, y) in enumerate(zip(out_r2, out_r3)) if x != y),
                min(len(out_r2), len(out_r3)),
            )
            lo, hi = max(0, idx - 40), idx + 40
            r.fail("SessionStart: at-most-once sweep change",
                   f"run2 != run3 at char {idx}: {out_r2[lo:hi]!r} vs {out_r3[lo:hi]!r}")


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

    check_sessionstart_determinism(cfg, r)
    check_cross_session_determinism(cfg, r)
    check_tool_hooks(r)
    check_static_volatility(r)

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
