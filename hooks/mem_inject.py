#!/usr/bin/env python3
# hooks/mem_inject.py — SessionStart: recovery sweep, then inject memories + refusal notice
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read_initialized_at(con):
    row = con.execute("SELECT value FROM schema_meta WHERE key='initialized_at'").fetchone()
    return row[0] if row else "1970-01-01T00:00:00Z"


def _cfg_recovery_budget():
    import json as _j
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "gates", "config.json")
    try:
        return _j.loads(open(p, encoding="utf-8").read()).get(
            "recovery_budget", {"max_bytes": 10485760, "max_ms": 4000})
    except Exception:
        return {"max_bytes": 10485760, "max_ms": 4000}


def main() -> None:
    # Kill switch: tombstone this session so no future sweep captures its transcript.
    # Key on the filename stem — the same key the sweep will derive (finding #1).
    # If the marker cannot be written, be LOUD (finding #2): still exit 0.
    if os.environ.get("CCMEM_DISABLED"):
        try:
            from ccmem.paths import resolve_home
            from ccmem.killswitch import mark_disabled, session_id_from_transcript
            payload = json.loads(sys.stdin.buffer.read() or b"{}")
            t = payload.get("transcript_path") if isinstance(payload, dict) else None
            if t:
                sid = session_id_from_transcript(t)
                if not mark_disabled(resolve_home(), sid):
                    print(json.dumps({
                        "hookSpecificOutput": {"hookEventName": "SessionStart"},
                        "systemMessage": "ccmem is disabled but could not record a "
                        "do-not-capture marker for this session; it may be captured later."}))
        except Exception:
            pass
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
        from ccmem.db import connect, log_hook_event
        from ccmem.paths import maybe_migrate, resolve_home
        from ccmem.recovery import recover_project
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

        # 1) recovery sweep BEFORE building injection so crash-session refusals
        #    surface now. project dir comes straight from the payload's
        #    transcript_path (finding #9) — no cwd-munging on the hook path.
        transcript = payload.get("transcript_path", "")
        project_dir = os.path.dirname(transcript) if transcript else None
        try:
            if project_dir and os.path.isdir(project_dir):
                rb = _cfg_recovery_budget()
                recover_project(con, home, project_dir, _read_initialized_at(con),
                                rb["max_bytes"], rb["max_ms"])
        except Exception:
            pass  # R1: a locked DB or sweep error must not break injection

        # 2) build injection block (refusal notice + memories) and print once.
        #    PURE READ of sigil_refusals — no mutation, so SessionStart stays
        #    deterministic in steady state (finding #3). Acknowledgement is a
        #    CLI action (Task 14).
        blocks = []
        try:
            n_ref = con.execute(
                "SELECT COUNT(*) FROM sigil_refusals WHERE acknowledged_at IS NULL"
            ).fetchone()[0]
        except Exception:
            n_ref = 0
        if n_ref:
            blocks.append(f"{n_ref} unreviewed !mem: refusal(s) (contained secrets) "
                          f"— run `ccmem list --refused`.")

        external_lines = []
        test_claude = os.environ.get("CCMEM_TEST_CLAUDE_MD")
        if test_claude:
            external_lines = [test_claude]

        memories = retrieve(con, pid)
        if memories:
            rendered = render(memories, root, source, external_lines=external_lines)
            if rendered:
                blocks.append(rendered)
            # mark_accessed fires AFTER render so this session's ranking snapshot
            # is unaffected; mutations only influence subsequent sessions.
            mark_accessed(con, [m.id for m in memories])

        _dur = int((time.monotonic() - _t0) * 1000)  # ccmem: cache-safe
        try:
            log_hook_event(con, "SessionStart", source, duration_ms=_dur)
        except Exception:
            pass
        con.close()

        if blocks:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "\n\n".join(blocks),
                }
            }))
    except Exception:
        return


if __name__ == "__main__":
    main()
