#!/usr/bin/env python3
# hooks/mem_flush.py — SessionEnd: WAL checkpoint only
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    if os.environ.get("CCMEM_DISABLED"):
        return
    _t0 = time.monotonic()
    try:
        sys.stdin.buffer.read()
    except Exception:
        return
    try:
        from ccmem.db import connect, log_hook_event
        from ccmem.paths import maybe_migrate, resolve_home

        home = resolve_home()
        maybe_migrate(home)
        db_path = os.path.join(home, "mem.db")
        if not os.path.exists(db_path):
            return
        con = connect(db_path)
        con.execute("PRAGMA wal_checkpoint(PASSIVE)")
        _dur = int((time.monotonic() - _t0) * 1000)
        try:
            log_hook_event(con, "SessionEnd", "checkpoint", duration_ms=_dur)
        except Exception:
            pass
        con.close()
    except Exception:
        return


if __name__ == "__main__":
    main()
