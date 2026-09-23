#!/usr/bin/env python3
# hooks/mem_snapshot.py — PreCompact: mark pending candidates is_pre_compact=1
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    if os.environ.get("CCMEM_DISABLED"):
        return
    try:
        raw = sys.stdin.buffer.read()
        json.loads(raw)
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
        con.execute(
            "UPDATE candidates SET is_pre_compact=1 WHERE status='pending' AND is_pre_compact=0"
        )
        con.commit()
        con.close()
    except Exception:
        return


if __name__ == "__main__":
    main()
