#!/usr/bin/env python3
"""Gate: resolve_project_root must never silently return cwd on a non-'not-a-repo'
failure.

A wrong answer that looks like a right one is exactly how the WinError-6 bug
mis-scoped memories: git spawn failed, the resolver swallowed it and returned
cwd, and memories were tagged with the wrong project. This gate pins the rule —
git missing / timeout / spawn error / unexpected git error must RAISE ScopingError;
only a genuine 'not a git repository' returns cwd.

Run: python gates/gate_scoping_strict.py
"""
from __future__ import annotations
import subprocess
import sys
from unittest import mock

from _common import REPO_ROOT, GateResult


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    r = GateResult("scoping strict")

    try:
        from ccmem.scoping import ScopingError, resolve_project_root
    except Exception as exc:  # noqa: BLE001
        r.fail("ccmem.scoping importable (with ScopingError)", str(exc))
        return r.report()
    r.ok("ScopingError defined")

    def _expect_raise(label, **patch_kwargs):
        with mock.patch("ccmem.scoping.subprocess.run", **patch_kwargs):
            try:
                got = resolve_project_root("/some/path")
            except ScopingError:
                r.ok(label)
            except Exception as exc:  # noqa: BLE001
                r.fail(label, f"raised {type(exc).__name__}, expected ScopingError")
            else:
                r.fail(label, f"returned {got!r} instead of raising (SILENT FALLBACK)")

    _expect_raise("raises on git spawn OSError (WinError-6 class)",
                  side_effect=OSError(6, "The handle is invalid"))
    _expect_raise("raises on git timeout",
                  side_effect=subprocess.TimeoutExpired("git", 5))
    _expect_raise("raises when git is missing",
                  side_effect=FileNotFoundError())
    _expect_raise("raises on unexpected git error (rc!=0, not 'not a repo')",
                  return_value=subprocess.CompletedProcess([], 128, stdout="", stderr="fatal: bad object"))
    _expect_raise("raises on empty git output",
                  return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))

    # Genuine 'not a git repository' is the ONLY case that legitimately returns cwd.
    not_repo = subprocess.CompletedProcess(
        [], 128, stdout="",
        stderr="fatal: not a git repository (or any of the parent directories): .git")
    with mock.patch("ccmem.scoping.subprocess.run", return_value=not_repo):
        try:
            got = resolve_project_root("/some/path")
        except Exception as exc:  # noqa: BLE001
            r.fail("returns cwd when genuinely not a repo", f"raised {type(exc).__name__}")
        else:
            if got == "/some/path":
                r.ok("returns cwd when genuinely not a repo")
            else:
                r.fail("returns cwd when genuinely not a repo", f"returned {got!r}")

    # Sanity: a real repo resolves without raising.
    try:
        root = resolve_project_root(str(REPO_ROOT))
        r.ok("resolves real repo root", root)
    except Exception as exc:  # noqa: BLE001
        r.fail("resolves real repo root", f"raised {type(exc).__name__}: {exc}")

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
