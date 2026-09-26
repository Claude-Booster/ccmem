#!/usr/bin/env python3
"""Gate: generate output is byte-identical across two calls with the same DB state.

The generated files land in the SessionStart prompt prefix via @import. If they
differ between otherwise-identical runs, every session pays a cache write. Two
generate calls — even with access_count mutated between them — must be identical.

Run: python gates/gate_generate_determinism.py
"""
from __future__ import annotations
import hashlib
import os
import sys
import tempfile
from pathlib import Path

from _common import REPO_ROOT, GateResult

SEED_N = 30


def _seed(db_path: str, project_root: str) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from ccmem.db import connect, migrate
    pid = hashlib.sha256(project_root.encode()).hexdigest()[:16]
    con = connect(db_path)
    migrate(con)
    for i in range(SEED_N):
        scope = "global" if i % 3 == 0 else ("user" if i % 3 == 1 else "project")
        con.execute(
            "INSERT OR IGNORE INTO memories "
            "(id, type, content, subject, scope, project_id, project_root, "
            "created_at, status, access_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"d-{i:03d}", "decision", f"Determinism seed {i}: approach {i % 4}.",
             f"s-{i}", scope, pid, project_root,
             f"2026-01-{(i % 28) + 1:02d}T00:00:00Z", "active", i * 3),
        )
    con.commit()
    con.close()


def _check(r: GateResult, label: str, a: str, b: str) -> None:
    if a == b:
        r.ok(f"{label}: byte-identical across 2 runs", f"{len(a)} chars")
        return
    idx = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    lo, hi = max(0, idx - 40), idx + 40
    r.fail(f"{label}: byte-identical across 2 runs",
           f"diverges at char {idx}: {a[lo:hi]!r} vs {b[lo:hi]!r}")


def main() -> int:
    r = GateResult("generate determinism")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = os.path.join(tmp, "mem.db")
        proj = os.path.join(tmp, "proj")
        os.makedirs(proj, exist_ok=True)
        _seed(db_path, proj)
        r.ok("test DB seeded", f"{SEED_N} rows")

        sys.path.insert(0, str(REPO_ROOT))
        try:
            from ccmem.db import connect
            from ccmem.generate import generate_global, generate_project
        except Exception as exc:
            r.fail("ccmem.generate importable", str(exc))
            return r.report()

        claude = Path(tmp) / "claude"
        con = connect(db_path)
        g1 = generate_global(con, claude_home=claude).read_text(encoding="utf-8")
        p1 = generate_project(con, proj, require_gitignore=False).read_text(encoding="utf-8")
        if not g1.strip():
            r.fail("run1 non-empty", "empty output proves nothing")
            return r.report()

        con.execute("UPDATE memories SET access_count = access_count + 1")
        con.commit()

        g2 = generate_global(con, claude_home=claude).read_text(encoding="utf-8")
        p2 = generate_project(con, proj, require_gitignore=False).read_text(encoding="utf-8")
        _check(r, "global file", g1, g2)
        _check(r, "project file", p1, p2)
        con.close()

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
