#!/usr/bin/env python3
# hooks/mem_retrieve.py — UserPromptSubmit: sigil capture + opt-in per-turn injection
import json
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    if os.environ.get("CCMEM_DISABLED"):
        return

    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw)
    except Exception:
        return

    try:
        from ccmem.capture import extract_sigil
        from ccmem.db import connect, log_hook_event, migrate
        from ccmem.paths import maybe_migrate, resolve_home
        from ccmem.redact import redact
        from ccmem.render import render
        from ccmem.retrieval import retrieve
        from ccmem.scoping import project_key, resolve_project_root
        from ccmem.supersession import maybe_supersede

        home = resolve_home()
        maybe_migrate(home)
        db_path = os.path.join(home, "mem.db")

        prompt = payload.get("prompt", "")
        cwd = payload.get("cwd", os.getcwd())
        session_id = payload.get("session_id", "unknown")

        root = resolve_project_root(cwd)
        pid, _ = project_key(root)

        output: dict = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}

        # --- log raw prompt for ccmem doctor verification ---
        if os.path.exists(db_path):
            try:
                _lcon = connect(db_path)
                log_hook_event(_lcon, "UserPromptSubmit", prompt)
                _lcon.close()
            except Exception:
                pass

        # --- sigil capture (always, regardless of CCMEM_PER_TURN) ---
        text, scope, _cleaned = extract_sigil(prompt)
        if text and os.path.exists(db_path):
            redacted = redact(text)
            if redacted != text:
                output["systemMessage"] = "ccmem: annotation contained a secret and was not stored."
            else:
                con = connect(db_path)
                migrate(con)
                mem_id = str(uuid.uuid4())   # ccmem: cache-safe
                now = datetime.now(timezone.utc).isoformat()  # ccmem: cache-safe
                con.execute(
                    "INSERT OR IGNORE INTO memories "
                    "(id, type, content, scope, project_id, project_root, created_at, status) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (mem_id, "preference", redacted, scope or "project",
                     pid, root, now, "active"),
                )
                con.commit()
                maybe_supersede(con, mem_id, None, pid)
                con.close()
                output["systemMessage"] = f"ccmem: captured — {redacted[:80]}"

        # --- per-turn retrieval (opt-in) ---
        if os.environ.get("CCMEM_PER_TURN") == "1" and os.path.exists(db_path):
            con = connect(db_path)
            memories = retrieve(con, pid, query=prompt[:200] if prompt else None)
            con.close()
            if memories:
                block = render(memories, root, "per-turn")
                output["hookSpecificOutput"]["additionalContext"] = block

        if output.get("systemMessage") or output["hookSpecificOutput"].get("additionalContext"):
            print(json.dumps(output))

    except Exception:
        return


if __name__ == "__main__":
    main()
