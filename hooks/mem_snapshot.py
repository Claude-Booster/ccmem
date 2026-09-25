#!/usr/bin/env python3
# hooks/mem_snapshot.py — PreCompact: capture transcript turns, mark is_pre_compact=1
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    if os.environ.get("CCMEM_DISABLED"):
        return
    _t0 = time.monotonic()  # ccmem: cache-safe
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return
    except Exception:
        return
    try:
        from ccmem.capture import capture_transcript
        from ccmem.db import connect, log_hook_event, migrate
        from ccmem.paths import maybe_migrate, resolve_home

        home = resolve_home()
        maybe_migrate(home)
        db_path = os.path.join(home, "mem.db")
        if not os.path.exists(db_path):
            return
        transcript = payload.get("transcript_path", "")
        session_id = payload.get("session_id", "unknown")
        con = connect(db_path)
        migrate(con)
        if transcript:
            capture_transcript(con, transcript, session_id, is_pre_compact=True)
        _dur = int((time.monotonic() - _t0) * 1000)  # ccmem: cache-safe
        try:
            log_hook_event(con, "PreCompact", "capture+snapshot", duration_ms=_dur)
        except Exception:
            pass
        con.close()
    except Exception:
        return


if __name__ == "__main__":
    main()
