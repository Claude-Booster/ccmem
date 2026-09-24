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
    """Read Windows Defender path exclusions from the registry.

    Tries the local Defender key first, then the Group Policy / Intune key.
    Returns lowercased list, or None if all reads fail (non-Windows, access denied).
    Policy exclusions (from Intune/GPO) live in a separate key and can override
    or coexist with local exclusions — read both.
    """
    try:
        import winreg
        keys_to_try = [
            r"SOFTWARE\Microsoft\Windows Defender\Exclusions\Paths",
            r"SOFTWARE\Policies\Microsoft\Windows Defender\Exclusions\Paths",
        ]
        all_paths: list[str] = []
        any_readable = False
        for key_path in keys_to_try:
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
                any_readable = True
                i = 0
                while True:
                    try:
                        name, _, _ = winreg.EnumValue(key, i)
                        all_paths.append(name.lower())
                        i += 1
                    except OSError:
                        break
                winreg.CloseKey(key)
            except Exception:
                continue
        return all_paths if any_readable else None
    except Exception:
        return None


_DEFENDER_STATE_FILE = "defender_state.json"


def _load_defender_state(home: str) -> dict:
    """Load previously confirmed Defender exclusion state from CCMEM_HOME."""
    import json as _json
    path = os.path.join(home, _DEFENDER_STATE_FILE)
    try:
        return _json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_defender_state(home: str, confirmed_paths: list[str], floor_ms: float) -> None:
    """Save confirmed Defender exclusion state to CCMEM_HOME."""
    import json as _json
    from datetime import datetime, timezone
    state = {
        "confirmed_paths": confirmed_paths,
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "confirmed_floor_ms": round(floor_ms),
    }
    try:
        Path(os.path.join(home, _DEFENDER_STATE_FILE)).write_text(
            _json.dumps(state, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _measure_spawn_floor_ms(runs: int = 5) -> float:
    """Measure interpreter spawn floor: subprocess.run([sys.executable, '-c', 'pass']).

    Returns median elapsed ms over `runs` trials. This is the irreducible per-turn
    cost every hook pays before doing any Python work. Does NOT include hook logic.
    """
    import subprocess as _sp
    import statistics as _stat
    import time as _time
    times = []
    for _ in range(runs):
        t0 = _time.perf_counter()
        _sp.run([sys.executable, "-c", "pass"], capture_output=True)
        times.append((_time.perf_counter() - t0) * 1000)
    return _stat.median(times)


def _check_defender_exclusions(home: str, spawn_floor_ms: float | None = None) -> None:
    """Check Defender path exclusions are in place and have not been reverted by policy.

    On Intune/Group Policy managed machines, Add-MpPreference can be silently reverted
    at the next policy refresh. This function tracks the last confirmed state in
    CCMEM_HOME/defender_state.json and flags when a confirmed exclusion disappears.

    Detection strategy (in order):
    1. Read both the local Defender key and the policy/Intune key from the registry.
    2. If readable: compare against the expected set; flag any that are missing.
       Save confirmed state when all expected exclusions are present.
    3. If not readable (access denied): fall back to timing evidence — if the
       spawn floor is >50% above the confirmed floor, the exclusion may have reverted.
    """
    py_exe = sys.executable
    lib_dir = os.path.join(os.path.dirname(py_exe), "Lib")

    # Only track the Lib exclusion. hooks-dir exclusion was explicitly decided against:
    # the hooks directory contains live, frequently-edited code on a corporate-synced
    # path — a permanent AV exception there is not worth ~231ms. See FACTS.md §11.
    intended = {lib_dir.lower(): lib_dir}

    current = _read_defender_exclusion_paths()
    saved_state = _load_defender_state(home)

    def _is_excluded(target: str, exclusion_list: list[str]) -> bool:
        t = target.lower()
        return any(t in e or e in t for e in exclusion_list)

    if current is not None:
        # Registry is readable — check each expected path
        present = {k: _is_excluded(v, current) for k, v in intended.items()}
        all_ok = all(present.values())
        for raw_key, path in intended.items():
            status = "OK" if present[raw_key] else "MISSING"
            print(f"  Defender exclusion — Python Lib [{status}]: {path}")

        # Flag reverts against previously confirmed state
        if saved_state.get("confirmed_paths"):
            prev_confirmed = set(p.lower() for p in saved_state["confirmed_paths"])
            reverted = [
                path for raw_key, path in intended.items()
                if raw_key in prev_confirmed and not present[raw_key]
            ]
            if reverted:
                at = saved_state.get("confirmed_at", "unknown time")[:19]
                print(f"  WARN: Exclusion(s) were confirmed at {at} but are now MISSING.")
                print("  WARN: Likely reverted by Intune or Group Policy refresh.")
                print("  WARN: Re-apply with admin PowerShell:")
                for path in reverted:
                    print(f'  WARN:   Add-MpPreference -ExclusionPath "{path}"')

        if all_ok and spawn_floor_ms is not None:
            _save_defender_state(home, list(intended.values()), spawn_floor_ms)
    else:
        # Registry unreadable — use timing as evidence
        print("  Defender exclusion — registry unreadable (admin required for HKLM)")
        if saved_state.get("confirmed_floor_ms") and spawn_floor_ms is not None:
            confirmed_floor = saved_state["confirmed_floor_ms"]
            at = saved_state.get("confirmed_at", "unknown time")[:19]
            ratio = spawn_floor_ms / confirmed_floor
            if ratio > 1.5:
                print(f"  WARN: Spawn floor is {spawn_floor_ms:.0f}ms vs confirmed {confirmed_floor}ms at {at}.")
                print("  WARN: >50% regression suggests Defender exclusion was reverted by policy.")
                print("  WARN: Verify with admin PowerShell: (Get-MpPreference).ExclusionPath")
            else:
                print(f"  Timing OK: floor {spawn_floor_ms:.0f}ms vs confirmed {confirmed_floor}ms at {at}  (no revert detected)")
        elif saved_state.get("confirmed_paths"):
            at = saved_state.get("confirmed_at", "unknown time")[:19]
            print(f"  Last confirmed: {at} — run doctor with admin rights to re-verify registry state.")
        else:
            print("  No confirmed baseline yet. Run doctor with admin rights after applying Lib exclusion to save baseline.")

    # Show apply instructions only when the registry confirms the exclusion is missing.
    # When the registry is unreadable, we cannot determine whether the exclusion is
    # applied — the timing-based revert detection above handles that case instead.
    if current is not None:
        missing_cmds = [v for k, v in intended.items() if not _is_excluded(v, current)]
        if missing_cmds:
            print("  WARN: Python Lib Defender path exclusion is missing — hook spawn cost ~37% higher.")
            print("  WARN: Apply with admin PowerShell:")
            for path in missing_cmds:
                print(f'  WARN:   Add-MpPreference -ExclusionPath "{path}"')
            print("  WARN: Python Lib is a read-only stdlib directory with no user-writable code.")


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

    # Measure live spawn floor and compare to thresholds.
    # This is the irreducible per-turn cost before any hook logic runs.
    # The 10s Stop timeout prevents data loss but does not make a 5s pause fast.
    import json as _json
    _cfg_path = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "gates" / "config.json"
    _daemon_cfg = {}
    try:
        _daemon_cfg = _json.loads(_cfg_path.read_text()).get("daemon_trigger", {})
    except Exception:
        pass
    _spawn_warn = _daemon_cfg.get("spawn_warn_ms", 2000)
    _spawn_daemon = _daemon_cfg.get("spawn_daemon_ms", 3500)
    print("Measuring spawn floor (5 runs of python -c pass)...", flush=True)
    _spawn_p50 = _measure_spawn_floor_ms(runs=5)
    _spawn_status = (
        "OK" if _spawn_p50 < _spawn_warn
        else ("WARN" if _spawn_p50 < _spawn_daemon else "DAEMON RECOMMENDED")
    )
    print(f"  spawn floor p50: {_spawn_p50:.0f}ms  [{_spawn_status}]  (warn>{_spawn_warn}ms  daemon>{_spawn_daemon}ms)")
    if _spawn_p50 >= _spawn_warn:
        print(f"  NOTE: 10s Stop timeout prevents data loss but does not make {_spawn_p50:.0f}ms pauses fast.")
        print( "  NOTE: Apply Defender path exclusions to reduce spawn cost (see below).")

    # Check whether Defender path exclusions are in place and have not been reverted.
    # Passes spawn floor so the saved baseline can be updated when all exclusions are OK.
    _check_defender_exclusions(home, spawn_floor_ms=_spawn_p50)

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
        _min_events = _daemon_cfg.get("min_hook_events", 50)
        if n_ups > 0:
            print(f"\n  Stop/UPS events: {n_stop}/{n_ups}", end="")
            if n_ups < _min_events:
                print(f"  (insufficient data — need {_min_events}+ UPS events for drop-rate conclusions)")
            else:
                drop_pct = max(0, (n_ups - n_stop) / n_ups * 100)
                warn_pct = _daemon_cfg.get("stop_drop_warn_pct", 2.0)
                print(f"  ({drop_pct:.1f}% apparent drop rate)")
                if drop_pct >= warn_pct:
                    print(f"  WARN: >{warn_pct:.0f}% Stop drop rate — some turns are losing candidate memories.")
                    print("  WARN: Check Stop hook timeout and system spawn latency.")
        else:
            print("\n  Stop/UPS events: 0/0  (no sessions recorded yet)")
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
