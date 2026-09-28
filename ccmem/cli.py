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
    from ccmem.scoping import resolve_project_root
    con = _db(args)
    # Canonicalize to the git repo root so project_id matches what generate and
    # retrieval compute. Without this, an add from a subdirectory (or a non-git
    # path form) is stored under a different project_id and silently never shows up.
    root = resolve_project_root(args.project_root or os.getcwd())
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
    maybe_supersede(con, mem_id, args.subject, pid, args.scope)
    con.close()
    print(f"Added: {mem_id}")


def cmd_list(args):
    if getattr(args, "refused", False):
        con = _db(args)
        rows = con.execute(
            "SELECT id, created_at, excerpt FROM sigil_refusals "
            "WHERE acknowledged_at IS NULL ORDER BY created_at"
        ).fetchall()
        if not rows:
            print("No unreviewed !mem: refusals."); con.close(); return
        now = datetime.now(timezone.utc).isoformat()
        for rid, created, excerpt in rows:
            print(f"[{created[:19]}] refused (secret): {excerpt}")
            con.execute("UPDATE sigil_refusals SET acknowledged_at=? WHERE id=?", (now, rid))
        con.commit(); con.close()
        print(f"\nAcknowledged {len(rows)} refusal(s).")
        return
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
    from ccmem.scoping import resolve_project_root
    con = _db(args)
    # Promoted candidates must carry the same canonical project_id that generate and
    # retrieval compute, or they never surface. (Was hardcoded "manual"/"." — a bug.)
    root = resolve_project_root(getattr(args, "project_root", None) or os.getcwd())
    pid = _project_id(root)
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
                (mem_id, "decision", content, "project", pid, root, now, "active"),
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
        # stdin=DEVNULL: inheriting an invalid parent stdin handle (pytest capture,
        # some hook contexts) raises OSError [WinError 6] on spawn (Windows).
        _sp.run([sys.executable, "-c", "pass"], capture_output=True, stdin=_sp.DEVNULL)
        times.append((_time.perf_counter() - t0) * 1000)
    return _stat.median(times)


def _check_defender_exclusions(
    home: str, spawn_floor_ms: float | None = None, save_baseline: bool = False
) -> None:
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
        if save_baseline and spawn_floor_ms is not None:
            # User has confirmed the Lib exclusion out-of-band (admin one-liner). Record
            # this floor as the baseline for future timing-regression detection. This is
            # the only path a non-admin user has to populate the baseline, since they can
            # never read the HKLM Defender key themselves.
            prev = saved_state.get("confirmed_floor_ms")
            _save_defender_state(home, list(intended.values()), spawn_floor_ms)
            print(f"  Baseline saved: floor {spawn_floor_ms:.0f}ms recorded as confirmed (registry not read; trusting your confirmation).")
            if prev:
                print(f"  (previous baseline was {prev}ms)")
            if spawn_floor_ms >= 2000:
                print(f"  WARN: {spawn_floor_ms:.0f}ms is high for a Lib-excluded floor (expected ~1200-1600ms).")
                print("  WARN: If the system is under load right now, re-run --save-baseline on a quiet system for a cleaner baseline.")
        elif saved_state.get("confirmed_floor_ms") and spawn_floor_ms is not None:
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
            print("  No confirmed baseline yet. Confirm Lib exclusion (admin: (Get-MpPreference).ExclusionPath),")
            print("  then run: python -m ccmem.cli doctor --save-baseline")

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


def cmd_capture(args):
    from ccmem.capture import capture_transcript, extract_sigil
    from ccmem.killswitch import is_disabled, mark_disabled, session_id_from_transcript
    from ccmem.paths import resolve_home
    from ccmem.transcript import iter_turn_pairs

    transcript_path = args.transcript
    home = resolve_home()
    session_id = args.session_id or session_id_from_transcript(transcript_path)

    if is_disabled(home, session_id):
        print(f"capture skipped: session {session_id[:8]} disabled by !mem:off")
        return

    # Scan for !mem:off sigil before processing any turns.
    # Any turn whose user text starts with !mem:off (any scope) disables
    # capture for the whole session and writes a tombstone so future sweeps
    # also skip it.
    for pair in iter_turn_pairs(transcript_path):
        text, _scope, _ = extract_sigil(pair.user_turn)
        if text is not None and text.strip().lower() == "off":
            ok = mark_disabled(home, session_id)
            status = "tombstoned" if ok else "WARNING: tombstone write failed"
            print(f"capture disabled: !mem:off at turn {pair.ordinal}; {status}")
            return

    con = _db(args)
    result = capture_transcript(con, transcript_path, session_id)
    con.close()

    print(
        f"candidates={result.candidates}  "
        f"sigil_memories={result.sigil_memories}  "
        f"refusals={result.refusals}"
    )
    if result.promptid_drift:
        print("WARN: promptId drift — transcript has no promptId on user records; ordinal fallback used")


def cmd_pin(args):
    con = _db(args)
    val = 0 if getattr(args, "unpin", False) else 1
    cur = con.execute("UPDATE memories SET pinned=? WHERE id=?", (val, args.id))
    con.commit()
    con.close()
    if cur.rowcount == 0:
        print(f"Not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    print(f"{'Unpinned' if val == 0 else 'Pinned'}: {args.id}")


def cmd_generate(args):
    from ccmem.generate import GLOBAL_CAP, PROJECT_CAP, generate_global, generate_project
    from ccmem.scoping import resolve_project_root

    con = _db(args)
    project_root = getattr(args, "project_root", None) or resolve_project_root(os.getcwd())
    project_only = getattr(args, "project_only", False)
    global_only = getattr(args, "global_only", False)

    if not project_only:
        path = generate_global(con, claude_home=Path(os.path.expanduser("~")) / ".claude")
        tokens = (len(path.read_text(encoding="utf-8")) + 3) // 4
        print(f"global:  {path}  (~{tokens} / {GLOBAL_CAP} tokens)")

    if not global_only:
        path = generate_project(con, project_root)
        tokens = (len(path.read_text(encoding="utf-8")) + 3) // 4
        print(f"project: {path}  (~{tokens} / {PROJECT_CAP} tokens)")

    con.close()


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
    _save_baseline = getattr(args, "save_baseline", False)
    _runs = 15 if _save_baseline else 5
    print(f"Measuring spawn floor ({_runs} runs of python -c pass)...", flush=True)
    _spawn_p50 = _measure_spawn_floor_ms(runs=_runs)
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
    _check_defender_exclusions(home, spawn_floor_ms=_spawn_p50, save_baseline=_save_baseline)

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

    else:
        print("\nNo hook_log entries yet.")
        print("Verify: hooks are registered in settings.json and a real session has run.")

    # Unrecovered transcripts — the data-loss signal that replaces the retired
    # Stop/UPS drop rate. Mirrors recover_project's stat-only checks.
    import glob as _glob, time as _time
    from ccmem.paths import project_transcript_dir
    from ccmem.killswitch import is_disabled, session_id_from_transcript
    from ccmem.transcript import normalize_transcript_path
    _init = con.execute("SELECT value FROM schema_meta WHERE key='initialized_at'").fetchone()
    _init = _init[0] if _init else "1970-01-01T00:00:00Z"
    _tdir = project_transcript_dir(os.getcwd())
    _awaiting = []
    if os.path.isdir(_tdir):
        for _f in _glob.glob(os.path.join(_tdir, "*.jsonl")):
            try:
                _st = os.stat(_f)
            except OSError:
                continue
            _m = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_st.st_mtime))
            if _m <= _init:
                continue
            if is_disabled(home, session_id_from_transcript(_f)):
                continue
            _row = con.execute(
                "SELECT updated_at FROM transcript_progress WHERE transcript_path=?",
                (normalize_transcript_path(_f),),
            ).fetchone()
            if _row is not None and _m <= _row[0]:
                continue
            _awaiting.append(_f)
    # No SessionStart hook fires under allowManagedHooksOnly, so list the paths and
    # the exact command — the user captures manually.
    print(f"\n  transcripts awaiting capture: {len(_awaiting)}")
    for _f in sorted(_awaiting, key=os.path.getmtime, reverse=True)[:10]:
        print(f"    {_f}")
    if len(_awaiting) > 10:
        print(f"    ... and {len(_awaiting) - 10} more")
    if _awaiting:
        print('  Capture with: python -m ccmem.cli capture "<path above>"')

    # Sigil refusals awaiting review.
    _nref = con.execute(
        "SELECT COUNT(*) FROM sigil_refusals WHERE acknowledged_at IS NULL"
    ).fetchone()[0]
    if _nref:
        print(f"  Unreviewed !mem: refusals: {_nref} (run `ccmem list --refused`)")
    else:
        print("  Unreviewed !mem: refusals: 0")

    # promptId drift: capture advanced a HWM (last_ordinal >= 0) but recorded no
    # promptId — the transcript schema had no promptId on user records.
    _drift = con.execute(
        "SELECT COUNT(*) FROM transcript_progress WHERE last_ordinal >= 0 AND last_prompt_id IS NULL"
    ).fetchone()[0]
    if _drift:
        print(f"  FAIL: {_drift} transcript(s) show promptId drift — no promptId on user "
              "records; capture is on ordinal fallback.")
    con.close()

    # Tombstoned sessions — skipped by !mem:off sigil.
    from ccmem.killswitch import list_disabled
    _disabled_dir = Path(home) / "disabled"
    _tombstoned = list_disabled(home)
    if _tombstoned:
        print(f"\n  Tombstoned sessions (!mem:off): {len(_tombstoned)}")
        for sid in _tombstoned[:10]:
            print(f"    {sid}")
        if len(_tombstoned) > 10:
            print(f"    ... and {len(_tombstoned) - 10} more")
        print(f"  To un-skip: delete the marker file from {_disabled_dir}")
        print( "  (deleting restores the session to awaiting-capture state)")
    else:
        print("\n  Tombstoned sessions (!mem:off): 0")

    # --- Option E @import health ---
    import re as _re
    from ccmem.scoping import resolve_project_root

    def _report_tier(mem_file: Path, claude_md: Path, import_line: str, gen_cmd: str):
        if mem_file.exists():
            txt = mem_file.read_text(encoding="utf-8")
            toks = (len(txt) + 3) // 4
            print(f"  file OK:   {mem_file}  (~{toks} tokens)")
            m = _re.search(r"<!-- ccmem: (\d+) of (\d+) memories shown", txt)
            if m and int(m.group(1)) < int(m.group(2)):
                print(f"  WARN: only {m.group(1)} of {m.group(2)} memories shown -- "
                      f"prune old memories or raise the cap ({gen_cmd}).")
        else:
            print(f"  file MISSING: {mem_file}  -> run: {gen_cmd}")
        if claude_md.exists():
            if import_line in claude_md.read_text(encoding="utf-8"):
                print(f"  @import OK: {import_line} present in {claude_md}")
            else:
                print(f"  @import MISSING: add '{import_line}' to {claude_md}")
        else:
            print(f"  {claude_md} not found (create it to enable @import)")

    print("\nOption E @import health:")
    _claude_home = Path(os.path.expanduser("~")) / ".claude"
    _report_tier(_claude_home / "ccmem-memories.md", _claude_home / "CLAUDE.md",
                 "@ccmem-memories.md", "python -m ccmem.cli generate --global-only")
    from ccmem.scoping import ScopingError
    try:
        _proj = resolve_project_root(os.getcwd())
        _report_tier(Path(_proj) / ".ccmem" / "memories.md", Path(_proj) / "CLAUDE.md",
                     "@.ccmem/memories.md", "python -m ccmem.cli generate --project-only")
    except ScopingError as exc:
        print(f"  project tier: SCOPING ERROR — {exc}")
        print("  (cannot resolve the git repo root here; fix git/cwd before generating the project file)")


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
    lst.add_argument("--refused", action="store_true",
                     help="show and acknowledge unreviewed !mem: refusals (contained secrets)")

    s = sub.add_parser("show", help="show memory + context")
    s.add_argument("id")

    d = sub.add_parser("delete", help="soft-delete (reversible)")
    d.add_argument("id")

    r = sub.add_parser("restore", help="restore a deleted memory")
    r.add_argument("id")

    rev = sub.add_parser("review", help="review pending candidates")
    rev.add_argument("--project-root", help="project root for promoted memories (default: git root of cwd)")

    cap = sub.add_parser("capture", help="capture memories from a transcript JSONL file")
    cap.add_argument("transcript", help="path to a Claude Code session JSONL transcript")
    cap.add_argument("--session-id", help="override auto-detected session ID (default: transcript filename stem)")

    inj = sub.add_parser("inject", help="preview injection block")
    inj.add_argument("--dry-run", action="store_true")
    inj.add_argument("--project-root")

    pn = sub.add_parser("pin", help="pin a memory so it survives cap truncation in generate")
    pn.add_argument("id")
    pn.add_argument("--unpin", action="store_true", help="remove the pin instead of adding it")

    gen = sub.add_parser("generate", help="write memory markdown files for @import")
    gen.add_argument("--global-only", action="store_true",
                     help="write only ~/.claude/ccmem-memories.md")
    gen.add_argument("--project-only", action="store_true",
                     help="write only <project>/.ccmem/memories.md")
    gen.add_argument("--project-root", help="override project root (default: git root of cwd)")

    doc = sub.add_parser("doctor", help="health check")
    doc.add_argument(
        "--save-baseline", action="store_true",
        help="record the current spawn floor as the confirmed Defender baseline. Use after "
             "manually confirming the Lib exclusion is applied (admin: (Get-MpPreference).ExclusionPath). "
             "For non-admin users who cannot read the HKLM registry directly. Measures 15 runs for stability.",
    )

    args = p.parse_args()
    dispatch = {
        "add": cmd_add, "list": cmd_list, "show": cmd_show,
        "delete": cmd_delete, "restore": cmd_restore,
        "review": cmd_review, "inject": cmd_inject, "capture": cmd_capture,
        "pin": cmd_pin, "generate": cmd_generate, "doctor": cmd_doctor,
    }
    if args.cmd not in dispatch:
        p.print_help()
        sys.exit(1)
    from ccmem.scoping import ScopingError
    try:
        dispatch[args.cmd](args)
    except ScopingError as exc:
        print(f"ERROR: could not resolve the project root: {exc}", file=sys.stderr)
        print("Refusing to guess the project scope. Fix git/cwd, or pass --project-root.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
