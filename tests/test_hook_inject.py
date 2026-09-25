import json, os, subprocess, sys, time
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "fixtures", "transcripts")

def _run(payload, env):
    return subprocess.run([sys.executable, os.path.join(REPO, "hooks", "mem_inject.py")],
        input=json.dumps(payload).encode(), capture_output=True, env=env, timeout=30)

def _mkdb(home):
    from ccmem.db import connect, migrate; migrate(connect(os.path.join(home, "mem.db")))

def test_disabled_session_writes_marker_and_captures_nothing(tmp_path):
    home = str(tmp_path / "home"); os.makedirs(home); _mkdb(home)
    proj = str(tmp_path / "proj"); os.makedirs(proj)
    import shutil; t = os.path.join(proj, "s.jsonl"); shutil.copy(os.path.join(FIX, "basic.jsonl"), t)
    env = {**os.environ, "CCMEM_HOME": home, "PYTHONPATH": REPO, "CCMEM_DISABLED": "1"}
    p = _run({"hook_event_name":"SessionStart","transcript_path": t,"session_id":"s1","cwd": proj}, env)
    assert p.returncode == 0
    from ccmem.killswitch import is_disabled
    assert is_disabled(home, "s") is True  # marker keyed on filename stem "s" (s.jsonl)

def test_disabled_marker_write_failure_warns(tmp_path):
    # CCMEM_HOME points at a file, so the marker dir cannot be created -> loud (finding #2)
    homefile = tmp_path / "homefile"; homefile.write_text("x")
    proj = str(tmp_path / "proj"); os.makedirs(proj)
    import shutil; t = os.path.join(proj, "s.jsonl"); shutil.copy(os.path.join(FIX, "basic.jsonl"), t)
    env = {**os.environ, "CCMEM_HOME": str(homefile), "PYTHONPATH": REPO, "CCMEM_DISABLED": "1"}
    p = _run({"hook_event_name":"SessionStart","transcript_path": t,"session_id":"s1","cwd": proj}, env)
    assert p.returncode == 0
    assert b"could not record" in p.stdout.lower()

def test_refusal_surfaced_on_next_start(tmp_path):
    home = str(tmp_path / "home"); os.makedirs(home); _mkdb(home)
    proj = str(tmp_path / "proj"); os.makedirs(proj)
    import shutil; t = os.path.join(proj, "s.jsonl")
    shutil.copy(os.path.join(FIX, "sigil_secret.jsonl"), t)
    # ensure s.jsonl mtime is clearly AFTER the DB's initialized_at so recovery's
    # stat-prefilter (mtime_iso <= initialized_at => skip) does NOT skip it.
    future = time.time() + 10
    os.utime(t, (future, future))
    env = {**os.environ, "CCMEM_HOME": home, "PYTHONPATH": REPO}
    p = _run({"hook_event_name":"SessionStart","transcript_path": os.path.join(proj,"new.jsonl"),
              "session_id":"s2","cwd": proj}, env)
    assert p.returncode == 0
    assert b"unreviewed" in p.stdout.lower() and b"refusal" in p.stdout.lower()
