import os
from ccmem.db import connect, migrate
from ccmem.capture import capture_transcript

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "transcripts")

def _db():
    con = connect(":memory:"); migrate(con); return con

def test_sigil_writes_durable_memory_not_candidate():
    con = _db()
    r = capture_transcript(con, os.path.join(FIX, "sigil.jsonl"), "s1")
    assert r.sigil_memories == 1
    assert con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    row = con.execute(
        "SELECT type, status, content FROM memories"
    ).fetchone()
    assert row[0] == "preference" and row[1] == "active"
    assert "always run gates" in row[2]

def test_sigil_with_secret_refused_and_recorded():
    con = _db()
    r = capture_transcript(con, os.path.join(FIX, "sigil_secret.jsonl"), "s1")
    assert r.refusals == 1 and r.sigil_memories == 0
    assert con.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM sigil_refusals").fetchone()[0] == 1
