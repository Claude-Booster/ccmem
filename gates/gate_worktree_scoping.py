#!/usr/bin/env python3
"""Gate: resolve_project_root returns same root from main checkout and a linked worktree."""
from __future__ import annotations
import subprocess
import sys
import tempfile
from pathlib import Path
from _common import REPO_ROOT, GateResult


def main() -> int:
    r = GateResult("worktree scoping")

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from ccmem.scoping import resolve_project_root
    except Exception as exc:
        r.fail("ccmem.scoping importable", str(exc))
        return r.report()
    r.ok("ccmem.scoping importable")

    root_from_main = resolve_project_root(str(REPO_ROOT))
    r.ok("resolve_project_root(main)", root_from_main)

    wt_path = None
    try:
        with tempfile.TemporaryDirectory(prefix="ccmem-wt-gate-") as tmpdir:
            wt_path = Path(tmpdir) / "wt"
            proc = subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_path)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode != 0:
                r.fail("git worktree add", f"rc={proc.returncode} stderr={proc.stderr.strip()!r}")
                return r.report()
            r.ok("git worktree add", str(wt_path))

            root_from_wt = resolve_project_root(str(wt_path))
            r.ok("resolve_project_root(worktree)", root_from_wt)

            if Path(root_from_wt) != Path(root_from_main):
                r.fail(
                    "roots match",
                    f"main={root_from_main!r}  worktree={root_from_wt!r}",
                )
            else:
                r.ok("roots match", root_from_main)

            import hashlib
            pid_main = hashlib.sha256(root_from_main.encode()).hexdigest()[:16]
            pid_wt = hashlib.sha256(root_from_wt.encode()).hexdigest()[:16]
            if pid_main != pid_wt:
                r.fail("project_id identical", f"main={pid_main}  wt={pid_wt}")
            else:
                r.ok("project_id identical", pid_main)

    finally:
        if wt_path and wt_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                timeout=10,
            )

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
