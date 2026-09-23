from __future__ import annotations
import argparse
import hashlib
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _db(args):
    from ccmem.db import connect, migrate
    from ccmem.paths import maybe_migrate, resolve_home
    home = resolve_home()
    maybe_migrate(home)
    Path(home).mkdir(parents=True, exist_ok=True)
    path = Path(home) / "mem.db"
    con = connect(str(path))
    migrate(con)
    return con


def _project_id(root: str) -> str:
    return hashlib.sha256(root.encode()).hexdigest()[:16]


def cmd_add(args):
    from ccmem.redact import redact
    from ccmem.supersession import maybe_supersede
    con = _db(args)
    root = args.project_root or os.getcwd()
    pid = _project_id(root)
    content = redact(args.content)
    if content != args.content:
        print("WARNING: content contained a secret pattern and was redacted.", file=sys.stderr)
    mem_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO memories (id, type, content, context, subject, scope, "
        "project_id, project_root, created_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (mem_id, args.type, content, args.context, args.subject,
         args.scope, pid, root, now, "active"),
    )
    con.commit()
    maybe_supersede(con, mem_id, args.subject, pid)
    con.close()
    print(f"Added: {mem_id}")


def cmd_list(args):
    con = _db(args)
    root = args.project_root or os.getcwd()
    pid = _project_id(root)
    rows = con.execute(
        "SELECT id, type, content, created_at FROM memories "
        "WHERE status='active' AND (scope='global' OR scope='user' OR project_id=?) "
        "ORDER BY created_at DESC",
        (pid,),
    ).fetchall()
    con.close()
    if not rows:
        print("(no memories)")
        return
    for rid, rtype, content, created in rows:
        print(f"{rid}  [{rtype}]  {content[:60]}  ({created[:10]})")


def cmd_show(args):
    con = _db(args)
    row = con.execute(
        "SELECT id, type, content, context, subject, scope, status, created_at "
        "FROM memories WHERE id=?", (args.id,)
    ).fetchone()
    con.close()
    if not row:
        print(f"Not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    rid, rtype, content, context, subject, scope, status, created = row
    print(f"ID:      {rid}")
    print(f"Type:    {rtype}  Scope: {scope}  Status: {status}")
    print(f"Created: {created[:10]}")
    if subject:
        print(f"Subject: {subject}")
    print(f"\n{content}")
    if context:
        print(f"\n--- context ---\n{context}")


def cmd_delete(args):
    con = _db(args)
    con.execute("UPDATE memories SET status='deleted' WHERE id=?", (args.id,))
    con.commit()
    con.close()
    print(f"Deleted (reversible): {args.id}")


def cmd_restore(args):
    con = _db(args)
    con.execute("UPDATE memories SET status='active' WHERE id=?", (args.id,))
    con.commit()
    con.close()
    print(f"Restored: {args.id}")


def cmd_review(args):
    con = _db(args)
    rows = con.execute(
        "SELECT id, session_id, user_turn, assistant_turn, classifier_score "
        "FROM candidates WHERE status='pending' ORDER BY created_at"
    ).fetchall()
    if not rows:
        print("No pending candidates.")
        con.close()
        return
    for cid, sess, user, asst, score in rows:
        print(f"\n--- candidate {cid} (score {score:.1f}) ---")
        print(f"User:   {user[:120]}")
        print(f"Claude: {asst[:120]}")
        action = input("Accept (a), Reject (r), Skip (s)? ").strip().lower()
        if action == "a":
            from ccmem.redact import redact
            content = input("Memory text (Enter to use assistant turn): ").strip() or asst[:200]
            content = redact(content)
            mem_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            con.execute(
                "INSERT INTO memories (id, type, content, scope, project_id, "
                "project_root, created_at, status) VALUES (?,?,?,?,?,?,?,?)",
                (mem_id, "decision", content, "project", "manual", ".", now, "active"),
            )
            con.execute("UPDATE candidates SET status='accepted' WHERE id=?", (cid,))
            con.commit()
            print(f"Stored: {mem_id}")
        elif action == "r":
            con.execute("UPDATE candidates SET status='rejected' WHERE id=?", (cid,))
            con.commit()
    con.close()


def cmd_inject(args):
    from ccmem.render import render
    from ccmem.retrieval import retrieve
    from ccmem.scoping import project_key, resolve_project_root
    con = _db(args)
    root = args.project_root or resolve_project_root(os.getcwd())
    pid, _ = project_key(root)
    memories = retrieve(con, pid)
    con.close()
    if not memories:
        print("(nothing to inject)")
        return
    block = render(memories, root, "dry-run")
    tokens = (len(block) + 3) // 4
    print(f"--- dry run ({len(memories)} memories, ~{tokens} tokens) ---")
    print(block)


def _check_store_stub() -> None:
    """Warn if any ccmem hook command uses the Windows Store Python stub."""
    import json as _json
    import re as _re
    settings_path = Path(os.path.expanduser("~")) / ".claude" / "settings.json"
    if not settings_path.exists():
        return
    try:
        cfg = _json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return
    hooks_cfg = cfg.get("hooks", {})
    # Pattern: command starts with bare "python " (Store stub on Windows PATH)
    stub_pattern = _re.compile(r'^python\s+"[^"]*ccmem', _re.IGNORECASE)
    real_py = sys.executable  # what gates use; what hooks should use
    stub_found = []
    for event, hook_list in hooks_cfg.items():
        for group in hook_list:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if stub_pattern.match(cmd):
                    stub_found.append((event, cmd[:80]))
    if stub_found:
        print(f"WARN: {len(stub_found)} hook command(s) use 'python' (Windows Store stub).")
        print(f"WARN: The stub re-execs to the real interpreter, adding 2-5x startup overhead.")
        print(f"WARN: Replace 'python' with the full path: {real_py}")
        for event, cmd in stub_found:
            print(f"WARN:   {event}: {cmd}...")
    else:
        print(f"python interpreter: {real_py}  (hooks use direct path, no Store stub)")


def _read_defender_exclusion_paths() -> list[str] | None:
    """Read Windows Defender path exclusions from the registry (no elevation needed).

    Returns a list of excluded paths (lowercased), or None if unavailable
    (non-Windows, registry key missing, or access denied).
    """
    try:
        import winreg
        key_path = r"SOFTWARE\Microsoft\Windows Defender\Exclusions\Paths"
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
        exclusions = []
        i = 0
        while True:
            try:
                name, _, _ = winreg.EnumValue(key, i)
                exclusions.append(name.lower())
                i += 1
            except OSError:
                break
        winreg.CloseKey(key)
        return exclusions
    except Exception:
        return None


def _check_defender_exclusions() -> None:
    """Check whether Defender path exclusions are in place for ccmem hooks."""
    hooks_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks"
    )
    py_exe = sys.executable
    lib_dir = os.path.join(os.path.dirname(py_exe), "Lib")

    current_exclusions = _read_defender_exclusion_paths()
    hooks_excluded = (
        current_exclusions is not None
        and any(hooks_dir.lower() in e or e in hooks_dir.lower() for e in current_exclusions)
    )
    lib_excluded = (
        current_exclusions is not None
        and any(lib_dir.lower() in e or e in lib_dir.lower() for e in current_exclusions)
    )

    if current_exclusions is not None:
        status_hooks = "OK" if hooks_excluded else "MISSING"
        status_lib = "OK" if lib_excluded else "MISSING"
        print(f"  Defender exclusion — hooks dir [{status_hooks}]: {hooks_dir}")
        print(f"  Defender exclusion — Python Lib [{status_lib}]: {lib_dir}")
    else:
        print("  Defender exclusion — cannot read state (admin required to read HKLM Defender config)")

    if current_exclusions is None or not hooks_excluded or not lib_excluded:
        print("  WARN: Defender file-path exclusions reduce hook spawn cost by ~37%.")
        print("  WARN: Apply with admin PowerShell (one-time, machine-level):")
        if current_exclusions is None or not hooks_excluded:
            print(f'  WARN:   Add-MpPreference -ExclusionPath "{hooks_dir}"')
        if current_exclusions is None or not lib_excluded:
            print(f'  WARN:   Add-MpPreference -ExclusionPath "{lib_dir}"')
        print("  WARN: These are read-only source paths with no user-writable code.")


def cmd_doctor(args):
    from ccmem.paths import resolve_home, sync_root_for
    home = resolve_home()
    db_path = Path(home) / "mem.db"
    print(f"CCMEM_HOME: {home}")
    print(f"DB path:    {db_path}")
    print(f"DB exists:  {db_path.exists()}")

    sync_root = sync_root_for(home)
    if sync_root:
        print(f"FAIL: CCMEM_HOME is inside a cloud sync root: {sync_root}", file=sys.stderr)
        print("FAIL: SQLite WAL mode is unsafe inside OneDrive/Dropbox/iCloud/Google Drive.", file=sys.stderr)
        print("FAIL: Set CCMEM_HOME to a local path, e.g. %LOCALAPPDATA%\\ccmem", file=sys.stderr)
        sys.exit(1)

    # Check whether the hook scripts themselves live inside a sync boundary.
    # Hooks in OneDrive pay ~3-5x interpreter startup cost during active sync
    # because the OS filter driver intercepts every subprocess spawn. This does
    # not break correctness but degrades Stop hook latency on every turn.
    hooks_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
    hooks_sync = sync_root_for(hooks_dir)
    if hooks_sync:
        print(f"WARN: hook scripts are inside a sync root: {hooks_sync}")
        print(f"WARN: hooks dir: {hooks_dir}")
        print("WARN: subprocess spawn cost is 3-5x higher during active sync.")
        print("WARN: Consider installing ccmem as a proper plugin (hooks land in ~/.claude/plugins, outside sync).")
    else:
        print(f"hooks dir: {hooks_dir}  (outside sync boundary)")

    # Check whether settings.json hook commands use the Windows Store stub.
    _check_store_stub()

    # Check whether Defender path exclusions are in place.
    # Path exclusions save ~37% on spawn cost during active OneDrive sync.
    # Doctor shows status and the exact command to apply; does NOT apply silently.
    _check_defender_exclusions()

    if not db_path.exists():
        print("Run: python -m ccmem.cli add ... to create it.")
        return
    con = _db(args)
    counts = con.execute(
        "SELECT status, COUNT(*) FROM memories GROUP BY status"
    ).fetchall()
    for status, n in counts:
        print(f"  memories [{status}]: {n}")
    pending = con.execute("SELECT COUNT(*) FROM candidates WHERE status='pending'").fetchone()[0]
    print(f"  candidates [pending]: {pending}")

    rows = con.execute(
        "SELECT event, recorded_at, excerpt, duration_ms FROM hook_log ORDER BY id DESC LIMIT 10"
    ).fetchall()
    if rows:
        print(f"\nLast {len(rows)} hook events (most recent first):")
        for event, recorded_at, excerpt, dur in rows:
            dur_str = f"  {dur}ms" if dur is not None else ""
            print(f"  [{recorded_at}] {event}{dur_str}  {excerpt[:80]}")

        # Stop drop rate: count Stop vs UserPromptSubmit events to detect timeouts.
        # A killed Stop hook leaves no hook_log entry — missing entries = lost candidates.
        counts = dict(con.execute(
            "SELECT event, COUNT(*) FROM hook_log GROUP BY event"
        ).fetchall())
        n_stop = counts.get("Stop", 0)
        n_ups = counts.get("UserPromptSubmit", 0)
        if n_ups > 0:
            drop_pct = max(0, (n_ups - n_stop) / n_ups * 100)
            print(f"\n  Stop/UPS ratio: {n_stop}/{n_ups} ({drop_pct:.0f}% apparent drop rate)")
            if drop_pct > 2:
                print("  WARN: >2% Stop drop rate — some turns may be losing candidate memories.")
                print("  WARN: Check Stop hook timeout and system spawn latency.")
    else:
        print("\nNo hook_log entries yet.")
        print("Verify: hooks are registered in settings.json and a real session has run.")
    con.close()


def main():
    p = argparse.ArgumentParser(prog="ccmem")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("add", help="add a memory directly")
    a.add_argument("--type", default="decision",
                   choices=["decision", "preference", "correction", "project_state", "gotcha"])
    a.add_argument("--content", required=True)
    a.add_argument("--context")
    a.add_argument("--subject")
    a.add_argument("--scope", default="project", choices=["project", "user", "global"])
    a.add_argument("--project-root")

    lst = sub.add_parser("list", help="list active memories")
    lst.add_argument("--project-root")

    s = sub.add_parser("show", help="show memory + context")
    s.add_argument("id")

    d = sub.add_parser("delete", help="soft-delete (reversible)")
    d.add_argument("id")

    r = sub.add_parser("restore", help="restore a deleted memory")
    r.add_argument("id")

    sub.add_parser("review", help="review pending candidates")

    inj = sub.add_parser("inject", help="preview injection block")
    inj.add_argument("--dry-run", action="store_true")
    inj.add_argument("--project-root")

    sub.add_parser("doctor", help="health check")

    args = p.parse_args()
    dispatch = {
        "add": cmd_add, "list": cmd_list, "show": cmd_show,
        "delete": cmd_delete, "restore": cmd_restore,
        "review": cmd_review, "inject": cmd_inject, "doctor": cmd_doctor,
    }
    if args.cmd not in dispatch:
        p.print_help()
        sys.exit(1)
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
