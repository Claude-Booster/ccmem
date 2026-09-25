# tests/test_capture_transcript.py
import os
from ccmem.db import connect, migrate
from ccmem.capture import capture_transcript, MAX_CANDIDATE_CHARS

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "transcripts")

def _db():
    con = connect(":memory:"); migrate(con); return con

def test_only_salient_enqueued():
    con = _db()
    r = capture_transcript(con, os.path.join(FIX, "basic.jsonl"), "s1")
    # turn 1 ("hello"/"hi there") scores 0; turn 2 scores >= threshold
    assert r.candidates == 1
    assert con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1

def test_idempotent_rerun():
    con = _db(); p = os.path.join(FIX, "basic.jsonl")
    capture_transcript(con, p, "s1")
    r2 = capture_transcript(con, p, "s1")
    assert r2.candidates == 0
    assert con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1

def test_hash_uses_full_text_not_truncated(tmp_path):
    # two salient turns identical for the first MAX_CANDIDATE_CHARS, divergent tail
    head = "we decided " + "x" * (MAX_CANDIDATE_CHARS + 50)
    p = tmp_path / "t.jsonl"
    lines = []
    for i, tail in enumerate(("ALPHA", "OMEGA")):
        lines.append(f'{{"type":"user","promptId":"p{i}","cwd":"C:\\\\p","message":{{"content":[{{"type":"text","text":{__import__("json").dumps(head + tail)}}}]}}}}')
        lines.append('{"type":"assistant","apiBlockIndex":0,"message":{"content":[{"type":"text","text":"the approach is X because it is simpler"}]}}')
    p.write_text("\n".join(lines), encoding="utf-8")
    con = _db()
    r = capture_transcript(con, str(p), "s1")
    assert r.candidates == 2  # full-text hash keeps them distinct
    stored = con.execute("SELECT LENGTH(user_turn) FROM candidates").fetchall()
    assert all(n <= MAX_CANDIDATE_CHARS for (n,) in stored)  # storage truncated

def test_rejected_not_reenqueued():
    con = _db(); p = os.path.join(FIX, "basic.jsonl")
    capture_transcript(con, p, "s1")
    con.execute("UPDATE candidates SET status='rejected'"); con.commit()
    # wipe HWM to force a full re-scan; hash must still block
    con.execute("DELETE FROM transcript_progress"); con.commit()
    r = capture_transcript(con, p, "s1")
    assert r.candidates == 0

def test_empty_transcript_no_drift(tmp_path):
    p = tmp_path / "e.jsonl"; p.write_text("", encoding="utf-8")
    r = capture_transcript(_db(), str(p), "s1")
    assert r.candidates == 0 and r.promptid_drift is False

def test_pre_install_turns_skipped_by_timestamp(tmp_path):
    import json as _j
    con = _db()
    init_at = con.execute(
        "SELECT value FROM schema_meta WHERE key='initialized_at'").fetchone()[0]
    def line(ts, txt, role="user", pid="p"):
        rec = {"type": role, "message": {"content": [{"type": "text", "text": txt}]}}
        if role == "user":
            rec["promptId"] = pid; rec["timestamp"] = ts; rec["cwd"] = "C:\\p"
        else:
            rec["apiBlockIndex"] = 0
        return _j.dumps(rec)
    p = tmp_path / "resumed.jsonl"
    p.write_text("\n".join([
        line("2000-01-01T00:00:00Z", "we decided to use OLD", "user", "p0"),
        line("", "the approach is OLD because it works", "assistant"),
        line("2999-01-01T00:00:00Z", "we decided to use NEW", "user", "p1"),
        line("", "the approach is NEW because it works", "assistant"),
    ]), encoding="utf-8")
    r = capture_transcript(con, str(p), "s1")
    assert r.candidates == 1
    (txt,) = con.execute("SELECT user_turn FROM candidates").fetchone()
    assert "NEW" in txt and "OLD" not in txt
