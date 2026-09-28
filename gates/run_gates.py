#!/usr/bin/env python3
"""Run ccmem gates.

    python gates/run_gates.py              # everything
    python gates/run_gates.py --phase 1    # gates that apply from phase 1 onward
    python gates/run_gates.py --only cache_safety
    python gates/run_gates.py --list

Exit code is 0 only if every selected gate passes. Gates are the acceptance
criteria for each phase -- if one is failing, fix the implementation. If a gate
encodes the wrong rule, say so and change it deliberately, in its own commit.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

GATES_DIR = Path(__file__).resolve().parent

# (module, min_phase, one-line purpose)
REGISTRY = [
    # Phase 0 gate
    ("gate_scaffold",         0, "ccmem importable from repo; pytest collects; no stale test DB; no old schema names; config hook names match DESIGN.md"),
    # Phase 1 gates
    # Hook-driving gates (gate_hook_contract, gate_cache_safety, gate_recovery_budget,
    # gate_injection_format) were retired 2026-09-26: allowManagedHooksOnly blocks all
    # ccmem hooks, so they tested never-invoked code. Injection is now @import of files
    # written by `ccmem generate`. The exit-0/hostile-input discipline moved to
    # tests/test_cli_robustness.py; format checks moved into gate_budget. See FACTS.md.
    ("gate_schema_contract",  1, "every table and column in DESIGN.md schema exists after migration"),
    ("gate_secret_hygiene",   1, "secrets redacted on write (content + context); DB gitignored"),
    ("gate_budget",           1, "injected context within token and top-K caps"),
    ("gate_generate_determinism", 1, "generate output is byte-identical across repeated calls"),
    ("gate_phase1_notes",     1, "PHASE1-NOTES.md exists with substance before Phase 2"),
    ("gate_fts5_retrieval",   1, "FTS5 query on seeded DB returns expected memories"),
    ("gate_overlap_dedup",    1, "memory matching external CLAUDE.md line is suppressed"),
    ("gate_subject_supersession", 1, "duplicate subject supersedes older row"),
    ("gate_worktree_scoping", 1, "resolve_project_root identical for main and linked worktree"),
    ("gate_scoping_strict",   1, "resolve_project_root raises (never silently returns cwd) on non-'not-a-repo' git failure"),
    # Phase 2+ gates get appended here as they're written. Suggested:
    # ("gate_retrieval_determinism",  2, "same query -> same ranking"),
    # ("gate_embedding_dim",          2, "refuse mixed-dimension reads"),
    # ("gate_threshold_calibration",  2, "KNN supersession >= 90% precision on labeled set"),
    # ("gate_supersession",           2, "contradictions chain; no orphan actives"),
    # ("gate_plugin_manifest",        4, "plugin installs and uninstalls cleanly"),
    # ("gate_eval_regression",        5, "Recall@5 and MRR do not regress"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", type=int, default=None, help="run gates applicable at this phase")
    ap.add_argument("--only", action="append", default=[], help="run a specific gate (repeatable)")
    ap.add_argument("--list", action="store_true", help="list gates and exit")
    args = ap.parse_args()

    if args.list:
        width = max(len(m) for m, _, _ in REGISTRY)
        for module, min_phase, purpose in REGISTRY:
            print(f"  phase>={min_phase}  {module.ljust(width)}  {purpose}")
        return 0

    selected = []
    for module, min_phase, purpose in REGISTRY:
        short = module.removeprefix("gate_")
        if args.only:
            if module in args.only or short in args.only:
                selected.append((module, purpose))
            continue
        if args.phase is None:
            selected.append((module, purpose))
        elif min_phase <= args.phase:
            selected.append((module, purpose))

    if not selected:
        print("No gates selected. Try --list.", file=sys.stderr)
        return 2


    results: list[tuple[str, bool]] = []
    for module, _purpose in selected:
        script = GATES_DIR / f"{module}.py"
        if not script.exists():
            print(f"\n=== {module} ===\n  [FAIL] gate script missing: {script}")
            results.append((module, False))
            continue
        proc = subprocess.run([sys.executable, str(script)], cwd=str(GATES_DIR))
        results.append((module, proc.returncode == 0))

    width = max(len(m) for m, _ in results)
    print("\n" + "=" * (width + 12))
    print("SUMMARY")
    print("=" * (width + 12))
    for module, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {module.ljust(width)}")

    failed = [m for m, ok in results if not ok]
    if failed:
        print(f"\n{len(failed)} gate(s) failing: {', '.join(failed)}")
        return 1
    print(f"\nAll {len(results)} gate(s) passing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
