#!/usr/bin/env python3
"""Gate: injected-context budget (CLAUDE.md R9, brief S7).

The failure mode this guards against is the one every memory tool hits: it works,
so you retrieve more, and then it works worse. "Lost in the Middle" and the
context-rot work both show recall degrading as retrieved context grows.

The budget is deliberately low. Raising it is a regression until an eval says
otherwise.

Run: python gates/gate_budget.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from _common import (
    REPO_ROOT,
    GateResult,
    base_payload,
    ensure_seeded_db,
    estimate_tokens,
    hook_path,
    injected_text,
    load_config,
    run_hook,
)

SEED_ROWS = 400  # far more than the cap, so truncation is actually exercised


def seed_db(cfg: dict, r: GateResult) -> Path | None:
    """Populate a throwaway DB so the budget check has something to truncate."""
    db, err = ensure_seeded_db(SEED_ROWS)
    if db is None:
        r.fail("test DB seeded", err)
        return None
    r.ok("test DB seeded", f"{SEED_ROWS} rows")
    return db


def main() -> int:
    cfg = load_config()
    r = GateResult("injection budget")

    script = hook_path(cfg, "SessionStart")
    if not script.exists():
        r.fail("SessionStart hook exists", cfg["hooks"]["SessionStart"])
        return r.report()

    if seed_db(cfg, r) is None:
        return r.report()

    run = run_hook(cfg, "SessionStart", base_payload("SessionStart"))
    if run.timed_out or run.returncode != 0:
        r.fail("SessionStart runs against seeded DB", f"rc={run.returncode}")
        return r.report()

    ctx = injected_text(run.stdout)
    if not ctx.strip():
        r.fail(
            "injects something",
            "400 seeded memories and nothing retrieved -- retrieval is broken",
        )
        return r.report()
    r.ok("injects something", f"{len(ctx)} chars")

    # --- token ceiling --------------------------------------------------
    tokens = estimate_tokens(ctx)
    cap = cfg["max_injected_tokens"]
    if tokens > cap:
        r.fail("within token budget", f"~{tokens} tokens > {cap} cap")
    else:
        r.ok("within token budget", f"~{tokens} tokens <= {cap}")

    # --- top-K ceiling ---------------------------------------------------
    # Count rendered memory entries. The renderer must emit one line-leading
    # marker per memory so this is countable without parsing prose.
    bullet = cfg.get("memory_line_prefix", "- ")
    count = sum(1 for line in ctx.splitlines() if line.startswith(bullet))
    if count == 0:
        r.fail(
            "memories are countable",
            f"no lines start with {bullet!r} -- renderer must mark each memory",
        )
    elif count > cfg["topk_cap"]:
        r.fail("within top-K cap", f"{count} memories > {cfg['topk_cap']} cap")
    else:
        r.ok("within top-K cap", f"{count} memories <= {cfg['topk_cap']}")

    # --- R9: delimited and labelled as data -----------------------------
    marker = cfg["injection_marker"]
    if marker not in ctx:
        r.fail(
            "injected block is delimited",
            f"missing {marker!r} -- memory content is untrusted and must be fenced",
        )
    else:
        r.ok("injected block is delimited", marker)

    closing = marker.replace("<", "</", 1)
    if closing not in ctx:
        r.fail("injected block is closed", f"missing {closing!r}")
    else:
        r.ok("injected block is closed")

    # --- graceful degradation --------------------------------------------
    missing_home = REPO_ROOT / ".ccmem-nonexistent"
    gone = run_hook(
        cfg, "SessionStart", base_payload("SessionStart"),
        env_extra={"CCMEM_HOME": str(missing_home)},
    )
    if gone.returncode != 0:
        r.fail("degrades when DB absent", f"rc={gone.returncode}")
    elif injected_text(gone.stdout).strip():
        r.fail("degrades when DB absent", "injected context with no database")
    else:
        r.ok("degrades when DB absent", "silent, exit 0")

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
