#!/usr/bin/env python3
"""Gate: generated-file token budget (DESIGN caps: global=400, project=800).

The cap applies to the WHOLE FILE as written — header, count line, memory body,
and footer all count. This gate seeds far more memories than either cap allows,
generates the files, and asserts the file-as-written stays within budget.

It also carries the well-formedness checks ported from the retired
gate_injection_format: both markers present, count line parseable, no partial
memory lines, and an empty DB still produces a valid stub. The concern outlived
the gate; it now rides on the generated file.

Run: python gates/gate_budget.py
"""
from __future__ import annotations
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

from _common import REPO_ROOT, GateResult, estimate_tokens

SEED_ROWS = 400
GLOBAL_CAP = 400
PROJECT_CAP = 800

_COUNT_RE = re.compile(r"<!-- ccmem: (\d+) of (\d+) memories shown \(\d+ token cap\) -->")
_MEM_LINE_RE = re.compile(r"^- \[\w+\] .+$")  # well-formed memory line; catches partials


def seed_mixed(db_path: str, project_root: str, n: int) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from ccmem.db import connect, migrate
    pid = hashlib.sha256(project_root.encode()).hexdigest()[:16]
    con = connect(db_path)
    migrate(con)
    for i in range(n):
        scope = "global" if i % 3 == 0 else ("user" if i % 3 == 1 else "project")
        con.execute(
            "INSERT OR IGNORE INTO memories "
            "(id, type, content, subject, scope, project_id, project_root, "
            "created_at, status, access_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"b-{i:04d}", "decision",
             f"Budget memory {i}: replacing legacy component {i % 7} with the new one.",
             f"subj-{i}", scope, pid, project_root,
             f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T00:00:00Z", "active", i),
        )
    con.commit()
    con.close()


def _check_file(r: GateResult, label: str, path: Path, cap: int) -> None:
    if not path.exists():
        r.fail(f"{label} written", f"missing: {path}")
        return
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        r.fail(f"{label} non-empty", "seeded DB but file is blank")
        return
    r.ok(f"{label} written", f"{len(text)} chars")

    toks = estimate_tokens(text)  # WHOLE FILE
    if toks > cap:
        r.fail(f"{label} within whole-file cap", f"~{toks} > {cap}")
    else:
        r.ok(f"{label} within whole-file cap", f"~{toks} <= {cap}")

    if "<!-- ccmem -->" not in text or "<!-- /ccmem -->" not in text:
        r.fail(f"{label} delimited", "missing <!-- ccmem --> / <!-- /ccmem -->")
    else:
        r.ok(f"{label} delimited")

    m = _COUNT_RE.search(text)
    if not m:
        r.fail(f"{label} count line parseable",
               "no '<!-- ccmem: N of M memories shown (C token cap) -->'")
    else:
        r.ok(f"{label} count line parseable", f"{m.group(1)} of {m.group(2)}")

    bad = [ln for ln in text.splitlines() if ln.startswith("- [") and not _MEM_LINE_RE.match(ln)]
    if bad:
        r.fail(f"{label} no partial memory lines", f"malformed: {bad[0]!r}")
    else:
        r.ok(f"{label} no partial memory lines")


def main() -> int:
    r = GateResult("generated-file budget")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = os.path.join(tmp, "mem.db")
        proj = os.path.join(tmp, "proj")
        os.makedirs(proj, exist_ok=True)
        seed_mixed(db_path, proj, SEED_ROWS)
        r.ok("test DB seeded", f"{SEED_ROWS} rows (global/user/project)")

        sys.path.insert(0, str(REPO_ROOT))
        try:
            from ccmem.db import connect, migrate
            from ccmem.generate import generate_global, generate_project
        except Exception as exc:
            r.fail("ccmem.generate importable", str(exc))
            return r.report()

        con = connect(db_path)
        g = generate_global(con, claude_home=Path(tmp) / "claude")
        _check_file(r, "global file", g, GLOBAL_CAP)
        p = generate_project(con, proj, require_gitignore=False)
        _check_file(r, "project file", p, PROJECT_CAP)

        # graceful degradation: empty DB still writes a valid stub
        empty = os.path.join(tmp, "empty.db")
        econ = connect(empty)
        migrate(econ)
        estub = generate_global(econ, claude_home=Path(tmp) / "empty_claude")
        etext = estub.read_text(encoding="utf-8")
        if "<!-- ccmem -->" in etext and "0 of 0 memories shown" in etext:
            r.ok("empty DB writes stub")
        else:
            r.fail("empty DB writes stub", "stub markers/count line missing")
        econ.close()
        con.close()

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
