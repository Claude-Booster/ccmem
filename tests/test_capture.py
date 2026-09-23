from ccmem.capture import score_turn, extract_sigil, enqueue_candidate
from ccmem.db import connect, migrate


def test_score_decision_phrase():
    score = score_turn("we decided to use RLS triggers", "Good choice.")
    assert score >= 3


def test_score_correction_phrase():
    score = score_turn("actually that's wrong", "You're right, sorry.")
    assert score >= 2


def test_score_low_for_ordinary_exchange():
    score = score_turn("what time is it?", "It is 3pm.")
    assert score < 2


def test_extract_sigil_bare():
    text, scope, cleaned = extract_sigil("!mem: chose RLS over app-layer")
    assert text == "chose RLS over app-layer"
    assert scope == "project"


def test_extract_sigil_global():
    text, scope, cleaned = extract_sigil("!mem[global]: port 8076 is firewalled")
    assert text == "port 8076 is firewalled"
    assert scope == "global"


def test_extract_sigil_absent():
    text, scope, cleaned = extract_sigil("normal prompt text")
    assert text is None
    assert scope is None
    assert cleaned == "normal prompt text"


def test_enqueue_candidate_inserts_row(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    enqueue_candidate(con, "sess-1", "prompt-1", "user text", "asst text", 4.0)
    rows = con.execute("SELECT * FROM candidates").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "sess-1"   # session_id
    assert rows[0][6] == 0          # is_pre_compact


def test_enqueue_pre_compact_sets_flag(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    enqueue_candidate(con, "sess-1", None, "u", "a", 5.0, is_pre_compact=True)
    row = con.execute("SELECT is_pre_compact FROM candidates").fetchone()
    assert row[0] == 1


def test_enqueue_atomicity_no_partial_row(tmp_path):
    """SQLite INSERT + COMMIT is atomic: a connection closed between INSERT and COMMIT
    leaves no partial rows in a second connection.

    This tests the Stop hook kill scenario: Claude Code can SIGKILL the hook process
    at any point. If the hook is killed after INSERT but before COMMIT, WAL mode
    guarantees the incomplete transaction is never visible to other readers.
    A successful commit is indivisible — all columns are present or the row doesn't
    exist.
    """
    db = tmp_path / "mem.db"
    con = connect(db)
    migrate(con)

    # Simulate a transaction that starts but never commits (hook killed mid-flight).
    con.execute(
        "INSERT INTO candidates "
        "(id, session_id, prompt_id, user_turn, assistant_turn, classifier_score, status, is_pre_compact, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("cand-killed", "sess-killed", None, "user text", "asst text", 4.0, "pending", 0, "2026-01-01T00:00:00Z"),
    )
    # Do NOT commit — simulate process kill. Close without committing.
    con.close()

    # A fresh reader must see no rows from the uncommitted transaction.
    con2 = connect(db)
    rows = con2.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    con2.close()
    assert rows == 0, "uncommitted INSERT must not be visible after connection close"
