#!/usr/bin/env python3
"""Gate: SessionStart recovery stays within its wall-clock budget even with a
deliberately oversized transcript present. Partial sweeps are safe."""
from __future__ import annotations

import os
import sys
import tempfile
import time

from _common import GateResult, REPO_ROOT, load_config


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    r = GateResult("recovery budget")
    try:
        from ccmem.db import connect, migrate
        from ccmem.recovery import recover_project
    except Exception as exc:  # noqa: BLE001
        r.fail("ccmem importable", f"{type(exc).__name__}: {exc}")
        return r.report()
    r.ok("ccmem importable")

    budget = load_config().get("recovery_budget", {"max_bytes": 10485760, "max_ms": 4000})

    with tempfile.TemporaryDirectory() as d:
        con = connect(os.path.join(d, "mem.db"))
        migrate(con)
        big = os.path.join(d, "oversized.jsonl")
        with open(big, "w", encoding="utf-8") as fh:
            for i in range(5000):
                fh.write('{"type":"user","promptId":"p%d","cwd":"C:\\\\p","message":{"content":[{"type":"text","text":"we decided X %d"}]}}\n' % (i, i))
                fh.write('{"type":"assistant","apiBlockIndex":0,"message":{"content":[{"type":"text","text":"the approach is X"}]}}\n')
        t0 = time.monotonic()
        recover_project(con, d, d, "2000-01-01T00:00:00Z", budget["max_bytes"], budget["max_ms"])
        elapsed_ms = (time.monotonic() - t0) * 1000
        limit = budget["max_ms"] * 2  # margin over the wall-clock budget for the final in-flight file
        if elapsed_ms <= limit:
            r.ok("recovery within budget", f"{elapsed_ms:.0f}ms <= {limit}ms")
        else:
            r.fail("recovery within budget", f"{elapsed_ms:.0f}ms > {limit}ms")
        con.close()

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
