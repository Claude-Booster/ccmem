#!/usr/bin/env python3
"""Gate: memory whose content matches an external CLAUDE.md line is suppressed."""
from __future__ import annotations
import hashlib
import sys
import tempfile
from pathlib import Path
from _common import REPO_ROOT, GateResult, base_payload, injected_text, load_config, run_hook

# The memory whose content overlaps the test external line.
OVERLAP_CONTENT = "chose approach 0 for subsystem 0"
OVERLAP_FULL = "Seed fact 0: chose approach 0 for subsystem 0."
CONTROL_CONTENT = "Latency under contention favored a connection pool over sequential writes."


def _insert(con, mem_id, content, pid, created_at):
    con.execute(
        "INSERT OR IGNORE INTO memories "
        "(id, type, content, scope, project_id, project_root, created_at, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (mem_id, "decision", content, "project", pid, str(REPO_ROOT), created_at, "active"),
    )


def main() -> int:
    r = GateResult("overlap dedup")

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from ccmem.db import connect, migrate
    except Exception as exc:
        r.fail("ccmem imports", str(exc))
        return r.report()

    pid = hashlib.sha256(str(REPO_ROOT).encode()).hexdigest()[:16]

    with tempfile.TemporaryDirectory(prefix="ccmem-dedup-gate-") as tmpdir:
        home = Path(tmpdir)
        db = home / "mem.db"

        con = connect(db)
        migrate(con)
        _insert(con, "dedup-overlap-0", OVERLAP_FULL, pid, "2026-01-01T00:00:00Z")
        _insert(con, "dedup-control-1", CONTROL_CONTENT, pid, "2026-01-02T00:00:00Z")
        con.commit()
        con.close()
        r.ok("test DB seeded", "2 memories: 1 overlapping, 1 control")

        cfg = load_config()
        payload = base_payload("SessionStart")

        run_with = run_hook(cfg, "SessionStart", payload, env_extra={
            "CCMEM_HOME": str(home),
            "CCMEM_TEST_CLAUDE_MD": OVERLAP_CONTENT,
        })
        text_with = injected_text(run_with.stdout)
        r.ok("hook ran with overlap env", f"rc={run_with.returncode}")

        run_without = run_hook(cfg, "SessionStart", payload, env_extra={
            "CCMEM_HOME": str(home),
        })
        text_without = injected_text(run_without.stdout)
        r.ok("hook ran without overlap env", f"rc={run_without.returncode}")

        if OVERLAP_CONTENT in text_with:
            r.fail("overlapping memory suppressed",
                   f"overlap phrase still appears with CCMEM_TEST_CLAUDE_MD set: {OVERLAP_CONTENT!r}")
        else:
            r.ok("overlapping memory suppressed", "overlap phrase absent from output")

        non_overlap_phrase = "connection pool over sequential"
        if non_overlap_phrase not in text_without:
            r.fail("non-overlapping memory present (baseline)",
                   f"control memory not in baseline output — "
                   f"output was: {text_without[:200]!r}")
        else:
            r.ok("non-overlapping memory present (baseline)")

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
