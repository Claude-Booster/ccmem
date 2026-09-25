#!/usr/bin/env python3
"""Gate: scaffold in place and C1 reconciliation complete.

Phase 0 exit check. Five silent-poison things:

1. ccmem imports from the repo, not from site-packages.
   Every other gate imports ccmem; a shadowed install makes them all fail
   confusingly.

2. pytest --collect-only exits 0 with at least one test collected.
   A suite that can't be collected looks green; exit 5 is a lie, not silence.

3. .ccmem-test/ does not exist at rest.
   It holds the old schema from pre-reconciliation smoke tests. If it
   survived it makes gate_budget fail three tasks later looking like a
   code bug.

4. No old column/table names remain in gates/ as SQL or Python references.
   Checks: body, kind, project_key, capture_queue.
   The C1 reconciliation said "every occurrence, not just the two I named."
   This proves it rather than trusting it.

5. Every hook name in config.json matches DESIGN.md's component list.
   Catches C2-class rename drift (mem_finalize -> mem_flush) at the
   boundary rather than at first run.

Run: python gates/gate_scaffold.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from _common import GATES_DIR, REPO_ROOT, GateResult, load_config

# Canonical hook component names from DESIGN.md §Implementation.
# If DESIGN.md renames a hook, update this list in the same commit.
DESIGN_HOOK_NAMES = {
    "mem_inject.py",
    "mem_flush.py",
    "mem_snapshot.py",
}

# Old skeleton column/table names that must not appear in gate code.
# Each entry: (label, compiled pattern for SQL/Python references).
# Patterns are intentionally strict to avoid false positives on common words:
#   body, kind  -> matched only as quoted string literals (column-name context)
#   project_key, capture_queue -> matched as bare identifiers (unique enough)
OLD_SCHEMA_PATTERNS = [
    ("body",          re.compile(r"""(?x)
        ['"]\bbody\b['"]       # quoted string literal: "body" or 'body'
        | \brow\[.body.\]      # subscript: row["body"] or row['body']
        | (?:SELECT|INSERT\s+INTO|WHERE|FROM)\b[^#\n]*\bbody\b  # SQL ref
    """)),
    ("kind",          re.compile(r"""(?x)
        ['"]\bkind\b['"]       # quoted: "kind" or 'kind'
        | \brow\[.kind.\]      # subscript
        | (?:SELECT|INSERT\s+INTO|WHERE)\b[^#\n]*\bkind\b       # SQL ref
    """)),
    ("project_key",   re.compile(r"\bproject_key\b")),
    ("capture_queue", re.compile(r"\bcapture_queue\b")),
]


def check_import(r: GateResult) -> None:
    """Check 1: ccmem imports from the repo, not site-packages."""
    result = subprocess.run(
        [
            sys.executable, "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(REPO_ROOT)!r}); "
                "import ccmem; "
                "print(ccmem.__file__)"
            ),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        r.fail(
            "import ccmem succeeds",
            f"python -c 'import ccmem' failed — "
            f"create ccmem/__init__.py first. stderr: {result.stderr.strip()[:120]}",
        )
        return
    r.ok("import ccmem succeeds")

    file_path = Path(result.stdout.strip())
    try:
        relative = file_path.relative_to(REPO_ROOT)
    except ValueError:
        r.fail(
            "ccmem resolves inside repo",
            f"ccmem.__file__ = {file_path}  (outside {REPO_ROOT}) "
            "— a site-packages install is shadowing the dev copy",
        )
        return
    r.ok("ccmem resolves inside repo", str(relative))


def check_pytest_collects(r: GateResult) -> None:
    """Check 2: pytest --collect-only exits 0 with >= 1 test."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--tb=no"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    combined = result.stdout + result.stderr

    if result.returncode != 0:
        r.fail(
            "pytest --collect-only exits 0",
            f"exit {result.returncode} — "
            + combined.strip().splitlines()[-1] if combined.strip() else "no output",
        )
        return
    r.ok("pytest --collect-only exits 0")

    # Count collected items from the summary line e.g. "3 tests collected"
    # or "<Module tests/test_foo.py>" lines in verbose output.
    count = 0
    for line in combined.splitlines():
        m = re.match(r"\s*(\d+)\s+test[s]?\s+collected", line)
        if m:
            count = int(m.group(1))
            break
        if re.match(r"<(?:Function|Method|Module)", line.strip()):
            count += 1

    if count < 1:
        r.fail(
            "at least one test collected",
            "0 tests collected — add a test file; "
            "a suite that never runs looks green",
        )
    else:
        r.ok("at least one test collected", f"{count} test(s)")


def check_no_stale_test_db(r: GateResult) -> None:
    """Check 3: .ccmem-test/ does not exist."""
    stale = REPO_ROOT / ".ccmem-test"
    if stale.exists():
        r.fail(
            ".ccmem-test/ absent at rest",
            f"{stale} exists — it holds the old pre-reconciliation schema "
            "and will make gate_budget fail later. Remove it: "
            f"rm -rf {stale}",
        )
    else:
        r.ok(".ccmem-test/ absent at rest")


def check_old_schema_names(r: GateResult) -> None:
    """Check 4: no old column/table names remain in gates/ as SQL/Python refs."""
    gate_files = sorted(
        p for p in GATES_DIR.glob("*.py") if p.name != "gate_scaffold.py"
    )
    hits: list[str] = []

    for path in gate_files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            # Skip pure comment lines — they may explain why old names were removed.
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for label, pattern in OLD_SCHEMA_PATTERNS:
                if pattern.search(line):
                    hits.append(
                        f"{path.name}:{lineno}: old name '{label}' — {stripped[:80]}"
                    )

    if hits:
        r.fail(
            "no old schema names in gates/",
            "C1 reconciliation incomplete — fix each hit:\n    "
            + "\n    ".join(hits),
        )
    else:
        r.ok(
            "no old schema names in gates/",
            f"scanned {len(gate_files)} gate files",
        )


def check_config_hook_names(r: GateResult) -> None:
    """Check 5: every hook path in config.json names a DESIGN.md component."""
    cfg = load_config()
    hooks: dict = cfg.get("hooks", {})
    bad: list[str] = []

    for event, path_str in hooks.items():
        name = Path(path_str).name
        if name not in DESIGN_HOOK_NAMES:
            bad.append(
                f"{event}: {path_str!r} → {name!r} not in DESIGN.md component list"
            )

    if bad:
        r.fail(
            "all config.json hook names match DESIGN.md",
            "C2-class drift detected — config.json names a hook not in "
            "DESIGN.md's component list. Fix config.json or update "
            "DESIGN_HOOK_NAMES in this gate:\n    " + "\n    ".join(bad),
        )
    else:
        r.ok(
            "all config.json hook names match DESIGN.md",
            f"{len(hooks)} hooks verified",
        )


def main() -> int:
    r = GateResult("scaffold")
    check_import(r)
    check_pytest_collects(r)
    check_no_stale_test_db(r)
    check_old_schema_names(r)
    check_config_hook_names(r)
    return r.report()


if __name__ == "__main__":
    sys.exit(main())
