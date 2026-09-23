#!/usr/bin/env python3
# hooks/mem_flush.py — SessionEnd: WAL checkpoint only
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    if os.environ.get("CCMEM_DISABLED"):
        return
    try:
        sys.stdin.buffer.read()
    except Exception:
        return
    try:
        from ccmem.db import connect
        from ccmem.paths import maybe_migrate, resolve_home

        home = resolve_home()
        maybe_migrate(home)
        db_path = os.path.join(home, "mem.db")
        if not os.path.exists(db_path):
            return
        con = connect(db_path)
        con.execute("PRAGMA wal_checkpoint(PASSIVE)")
        con.close()
    except Exception:
        return


if __name__ == "__main__":
    main()
