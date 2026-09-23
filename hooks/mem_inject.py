#!/usr/bin/env python3
# hooks/mem_inject.py — SessionStart: retrieve memories → additionalContext
import json
import os
import sys

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
        from ccmem.db import connect, migrate
        from ccmem.paths import maybe_migrate, resolve_home
        from ccmem.render import render
        from ccmem.retrieval import mark_accessed, retrieve
        from ccmem.scoping import project_key, resolve_project_root

        home = resolve_home()
        maybe_migrate(home)
        db_path = os.path.join(home, "mem.db")
        if not os.path.exists(db_path):
            return

        cwd = payload.get("cwd", os.getcwd())
        root = resolve_project_root(cwd)
        pid, _ = project_key(root)
        source = payload.get("source", "startup")

        con = connect(db_path)
        memories = retrieve(con, pid)
        if not memories:
            con.close()
            return

        mark_accessed(con, [m.id for m in memories])
        con.close()

        external_lines = []
        test_claude = os.environ.get("CCMEM_TEST_CLAUDE_MD")
        if test_claude:
            external_lines = [test_claude]
        block = render(memories, root, source, external_lines=external_lines)
        if not block:
            return

        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": block,
            }
        }))
    except Exception:
        return


if __name__ == "__main__":
    main()
