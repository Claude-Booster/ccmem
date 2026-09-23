#!/usr/bin/env python3
# hooks/mem_capture.py — Stop: score turn → enqueue candidate (<200ms)
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
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw)
    except Exception:
        return

    if not isinstance(payload, dict):
        return

    if payload.get("stop_hook_active"):
        return  # already in a Stop loop — don't re-trigger

    try:
        from ccmem.capture import enqueue_candidate, score_turn
        from ccmem.db import connect, log_hook_event
        from ccmem.paths import maybe_migrate, resolve_home

        home = resolve_home()
        maybe_migrate(home)
        db_path = os.path.join(home, "mem.db")
        if not os.path.exists(db_path):
            return

        assistant_msg = payload.get("last_assistant_message", "")
        session_id = payload.get("session_id", "unknown")
        prompt_id = payload.get("prompt_id")

        user_turn = ""
        transcript = payload.get("transcript_path", "")
        if transcript and os.path.exists(transcript):
            try:
                import json as _json
                lines = open(transcript, encoding="utf-8", errors="replace").readlines()
                for line in reversed(lines):
                    try:
                        rec = _json.loads(line)
                        if rec.get("type") == "user":
                            content = rec.get("message", {}).get("content", [])
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    user_turn = block["text"]
                                    break
                            if user_turn:
                                break
                    except Exception:
                        continue
            except Exception:
                pass

        score = score_turn(user_turn, assistant_msg)
        threshold = float(os.environ.get("CCMEM_THRESHOLD", "4"))
        if score < threshold:
            return

        con = connect(db_path)
        enqueue_candidate(con, session_id, prompt_id, user_turn, assistant_msg, score)
        _dur = int((time.monotonic() - _t0) * 1000)
        try:
            log_hook_event(con, "Stop", assistant_msg[:100], duration_ms=_dur)
        except Exception:
            pass
        con.close()

    except Exception:
        return


if __name__ == "__main__":
    main()
