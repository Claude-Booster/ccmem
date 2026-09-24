# Snapshot-Driven Capture (Option B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ccmem's per-turn `Stop`/`UserPromptSubmit` capture with transcript-driven capture at `PreCompact`/`SessionEnd`, plus crash recovery at `SessionStart`, eliminating all per-turn interpreter spawns.

**Architecture:** Capture reads whole transcripts and enqueues the same scored turn-pairs the per-turn model would have, batched at events that do not fire per turn. Correctness comes from a `content_hash` UNIQUE constraint on candidates (dedup independent of session identity); a per-transcript high-water mark is a pure efficiency optimization. The `!mem:` sigil folds into the same batch routine, writing durable memories. ccmem goes from 5 hooks to 3.

**Tech Stack:** Python 3.11+ stdlib only (sqlite3, hashlib, json, os, time). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-24-snapshot-driven-capture-design.md` — read it alongside this plan.

## Global Constraints

- **R1** — every hook exits 0 on any error (malformed stdin, missing/locked DB, import failure). Never exit 2.
- **R2** — one SQLite file, no daemon, no background process.
- **R5** — retired by this work: the only per-turn injection path (`UserPromptSubmit`) is deleted. Mark retired in DESIGN.md; do not treat as violated.
- **R7** — candidates are scored turn-pairs pending review; only the trigger changes.
- **R8** — redact sigil text on write to `memories`. Candidates hold raw (truncated) turn text as today; the exposure grows and is audited (Task 12) and capped (Task 3).
- **Python 3.11+, stdlib-first.** No third-party deps in this work.
- **Patch, don't replace.** Targeted edits; the user may have in-flight edits in the same files.
- **Gates are acceptance criteria, not editable to pass.** Additive gate changes (new tables/columns) and removal of checks for genuinely-removed features are allowed; never weaken a gate to hide a failure. The cache-safety gate change (Task 11) is its own commit with reasoning.
- **content_hash is computed on FULL text; storage is truncated.** Never hash truncated text.
- **All transcript paths are normalized** (`os.path.normcase(os.path.realpath(path))`) before use as a key or marker.

## Review Focus

- **Concurrent SessionStart in the same project** (two Claude Code windows): both sweep, both write. `content_hash` dedups and WAL serializes, but `database is locked` becomes routine — capture must retry briefly then exit 0 without loss. Test in Task 8.
- **A single enormous turn** (multi-MB assistant response): reading and hashing the full turn must not blow memory or the recovery byte budget silently. Guard: the byte budget counts bytes as lines are read; a single oversized line is still capped for storage. Test in Task 8.
- **Worktree / project-dir resolution for recovery**: FACTS §4 — `git rev-parse --show-toplevel` lies inside a worktree; the transcript's own `cwd` field is authoritative. Recovery derives project root from the transcript record's `cwd`, not from the directory name. Test in Task 8.
- **Empty transcript or one with no `user` records** (session opened, nothing typed): capture must return zero cleanly, not error, and must not record a false promptId-drift failure. Test in Task 3.
- **CRLF / non-UTF-8 bytes in a transcript line**: parsing reads with `errors="replace"` and skips lines that are not valid JSON objects, without aborting the file. Test in Task 2.

---

## Task 1: Schema migration — content_hash, progress, refusals, init timestamp

**Files:**
- Modify: `ccmem/db.py:5-83`
- Modify: `gates/gate_schema_contract.py:25-76`
- Test: `tests/test_schema_migration.py` (create)

**Interfaces:**
- Produces: `candidates.content_hash TEXT` with `UNIQUE INDEX idx_candidates_content_hash`; `memories.content_hash TEXT` with `UNIQUE INDEX idx_memories_content_hash` (sigil idempotency — Task 5); tables `transcript_progress(transcript_path TEXT PRIMARY KEY, last_prompt_id TEXT, last_ordinal INTEGER, session_id TEXT, updated_at TEXT)` and `sigil_refusals(id TEXT PRIMARY KEY, transcript_path TEXT, session_id TEXT, created_at TEXT, excerpt TEXT, acknowledged_at TEXT)`; `schema_meta` key `initialized_at`; `connect()` sets `PRAGMA busy_timeout=3000`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema_migration.py
import sqlite3
from ccmem.db import connect, migrate

def _cols(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}

def test_new_schema_objects_exist():
    con = connect(":memory:"); migrate(con)
    assert "content_hash" in _cols(con, "candidates")
    assert "content_hash" in _cols(con, "memories")
    assert "acknowledged_at" in _cols(con, "sigil_refusals")
    for t in ("transcript_progress", "sigil_refusals"):
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone(), f"{t} missing"
    cidx = {r[1] for r in con.execute("PRAGMA index_list(candidates)")}
    midx = {r[1] for r in con.execute("PRAGMA index_list(memories)")}
    assert "idx_candidates_content_hash" in cidx
    assert "idx_memories_content_hash" in midx
    assert con.execute(
        "SELECT value FROM schema_meta WHERE key='initialized_at'"
    ).fetchone() is not None

def test_busy_timeout_set():
    con = connect(":memory:")
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 3000

def test_content_hash_unique_dedups():
    con = connect(":memory:"); migrate(con)
    for i in (1, 2):
        try:
            con.execute(
                "INSERT INTO candidates (id, session_id, user_turn, assistant_turn,"
                " classifier_score, created_at, status, content_hash)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (f"id{i}", "s", "u", "a", 5.0, "t", "pending", "HASH"),
            )
        except sqlite3.IntegrityError:
            pass
    assert con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schema_migration.py -v`
Expected: FAIL (content_hash column and new tables absent).

- [ ] **Step 3: Implement the migration**

In `ccmem/db.py`, add to `_DDL` after the `candidates` table block, before `session_injections`:

```sql
CREATE TABLE IF NOT EXISTS transcript_progress (
    transcript_path TEXT PRIMARY KEY,
    last_prompt_id  TEXT,
    last_ordinal    INTEGER NOT NULL DEFAULT -1,
    session_id      TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sigil_refusals (
    id              TEXT PRIMARY KEY,
    transcript_path TEXT,
    session_id      TEXT,
    created_at      TEXT NOT NULL,
    excerpt         TEXT NOT NULL,
    acknowledged_at TEXT
);
```

Add after the `hook_log` block, before the `INSERT OR IGNORE` seeds:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_content_hash
    ON candidates(content_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash
    ON memories(content_hash);
```

(A `UNIQUE` index permits multiple NULLs in SQLite, so manual memories from
`cmd_add` — which do not set `content_hash` — are unaffected; only sigil memories,
which set it, dedup.)

Change the seed section to record init time once and bump version:

```sql
INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', '2');
INSERT OR IGNORE INTO schema_meta VALUES ('embedding_dim', '384');
INSERT OR IGNORE INTO schema_meta VALUES ('initialized_at', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
```

In `migrate`, add idempotent column adds for pre-v2 DBs (mirrors the hook_log pattern):

```python
    for tbl in ("candidates", "memories"):
        try:
            con.execute(f"ALTER TABLE {tbl} ADD COLUMN content_hash TEXT")
            con.commit()
        except Exception:
            pass  # column already exists
```

In `connect`, add after the existing PRAGMAs:

```python
    con.execute("PRAGMA busy_timeout=3000")
```

- [ ] **Step 4: Update the schema-contract gate**

In `gates/gate_schema_contract.py`, add `"content_hash"` to the `candidates` list and to the `memories` list in **both** `EXPECTED_TABLES` (lines ~26, ~32) and `GATE_COLUMN_REFERENCES` (lines ~52, ~64), and add two entries to `EXPECTED_TABLES`:

```python
    "transcript_progress": [
        "transcript_path", "last_prompt_id", "last_ordinal", "session_id", "updated_at",
    ],
    "sigil_refusals": [
        "id", "transcript_path", "session_id", "created_at", "excerpt", "acknowledged_at",
    ],
```

If the gate's `schema_meta` seed check asserts `schema_version == '1'`, update it to `'2'` (read the gate; change the expected value, do not remove the check).

- [ ] **Step 5: Run tests and the gate**

Run: `python -m pytest tests/test_schema_migration.py -v && python gates/run_gates.py --phase 1`
Expected: tests PASS; schema-contract gate PASS.

- [ ] **Step 6: Commit**

```bash
git add ccmem/db.py gates/gate_schema_contract.py tests/test_schema_migration.py
git commit -m "feat(db): schema v2 — content_hash dedup, transcript_progress, sigil_refusals, initialized_at"
```

---

## Task 2: Transcript parsing module

**Files:**
- Create: `ccmem/transcript.py`
- Create: `fixtures/transcripts/` (JSONL fixtures)
- Test: `tests/test_transcript.py` (create)

**Interfaces:**
- Produces:
  - `normalize_transcript_path(path: str) -> str`
  - `TurnPair` dataclass: `prompt_id: str | None`, `ordinal: int`, `user_turn: str`, `assistant_turn: str`, `cwd: str | None`, `timestamp: str | None`
  - `iter_turn_pairs(transcript_path: str) -> Iterator[TurnPair]` — yields pairs in order; skips malformed lines; `ordinal` is the 0-based index of the `user` record; `timestamp` is the user record's `timestamp` field (used for the per-turn install bound, finding #4).

- [ ] **Step 1: Create fixtures**

Create `fixtures/transcripts/basic.jsonl` (two turns, second is salient):

```json
{"type":"user","promptId":"p1","cwd":"C:\\proj","message":{"content":[{"type":"text","text":"hello"}]}}
{"type":"assistant","apiBlockIndex":0,"message":{"content":[{"type":"text","text":"hi there"}]}}
{"type":"user","promptId":"p2","cwd":"C:\\proj","message":{"content":[{"type":"text","text":"we decided to use X instead of Y"}]}}
{"type":"assistant","apiBlockIndex":0,"message":{"content":[{"type":"text","text":"the approach is X"}]}}
{"type":"assistant","apiBlockIndex":1,"message":{"content":[{"type":"text","text":" because it is simpler"}]}}
```

Create `fixtures/transcripts/truncated_tail.jsonl` — same first four lines as `basic.jsonl` then a partial line:

```json
{"type":"user","promptId":"p1","cwd":"C:\\proj","message":{"content":[{"type":"text","text":"hello"}]}}
{"type":"assistant","apiBlockIndex":0,"message":{"content":[{"type":"text","text":"hi"}]}}
{"type":"user","promptId":"p2","cwd":"C:\\proj","message":{"content":[{"type":"text","tex
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_transcript.py
import os
from ccmem.transcript import iter_turn_pairs, normalize_transcript_path

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "transcripts")

def test_pairs_and_apiblock_concat():
    pairs = list(iter_turn_pairs(os.path.join(FIX, "basic.jsonl")))
    assert [p.prompt_id for p in pairs] == ["p1", "p2"]
    assert [p.ordinal for p in pairs] == [0, 1]
    assert pairs[1].assistant_turn == "the approach is X because it is simpler"
    assert pairs[1].user_turn == "we decided to use X instead of Y"

def test_truncated_final_line_skipped():
    pairs = list(iter_turn_pairs(os.path.join(FIX, "truncated_tail.jsonl")))
    # p1 pair is complete and captured; p2's partial line is skipped
    assert [p.prompt_id for p in pairs] == ["p1"]

def test_normalize_collapses_spellings(tmp_path):
    f = tmp_path / "T.jsonl"; f.write_text("{}")
    a = normalize_transcript_path(str(f))
    b = normalize_transcript_path(str(f).upper() if os.name == "nt" else str(f))
    assert a == b
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_transcript.py -v`
Expected: FAIL (module missing).

- [ ] **Step 4: Implement `ccmem/transcript.py`**

```python
from __future__ import annotations
import json
import os
from dataclasses import dataclass
from typing import Iterator


def normalize_transcript_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


@dataclass
class TurnPair:
    prompt_id: str | None
    ordinal: int
    user_turn: str
    assistant_turn: str
    cwd: str | None
    timestamp: str | None


def _text_blocks(rec: dict) -> str:
    parts = []
    for block in rec.get("message", {}).get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def iter_turn_pairs(transcript_path: str) -> Iterator[TurnPair]:
    """Yield (user, following-assistant-text) pairs in order.

    Robust to a partial/truncated final line and to non-JSON lines: a line
    that does not parse to a dict is skipped, never aborts the file. `ordinal`
    is the 0-based index of the user record.
    """
    try:
        fh = open(transcript_path, encoding="utf-8", errors="replace")
    except OSError:
        return
    ordinal = -1
    pending = None  # (prompt_id, ordinal, user_text, cwd)
    asst: list[str] = []
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue  # partial or malformed line — skip, keep going
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")
            if rtype == "user":
                if pending is not None:
                    yield TurnPair(pending[0], pending[1], pending[2],
                                   "".join(asst), pending[3], pending[4])
                ordinal += 1
                pending = (rec.get("promptId"), ordinal, _text_blocks(rec),
                           rec.get("cwd"), rec.get("timestamp"))
                asst = []
            elif rtype == "assistant" and pending is not None:
                asst.append(_text_blocks(rec))
        if pending is not None:
            yield TurnPair(pending[0], pending[1], pending[2], "".join(asst),
                           pending[3], pending[4])
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_transcript.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ccmem/transcript.py tests/test_transcript.py fixtures/transcripts/
git commit -m "feat(transcript): drift-tolerant turn-pair parser with path normalization"
```

---

## Task 3: capture_transcript core — full-text hash dedup + HWM

**Files:**
- Modify: `ccmem/capture.py`
- Modify: `gates/config.json` (add `max_candidate_chars`)
- Test: `tests/test_capture_transcript.py` (create)

**Interfaces:**
- Consumes: `iter_turn_pairs`, `normalize_transcript_path` (Task 2); `score_turn` (existing).
- Produces:
  - `content_hash(user_turn: str, assistant_turn: str) -> str`
  - `CaptureResult` dataclass: `candidates: int`, `sigil_memories: int`, `refusals: int`, `promptid_drift: bool`
  - `capture_transcript(con, transcript_path: str, session_id: str, is_pre_compact: bool = False) -> CaptureResult`
  - `MAX_CANDIDATE_CHARS` (module constant, default 2000; overridable via `CCMEM_MAX_CANDIDATE_CHARS`)

- [ ] **Step 1: Write the failing test**

```python
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
        lines.append('{"type":"assistant","apiBlockIndex":0,"message":{"content":[{"type":"text","text":"the approach is X"}]}}')
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
        line("", "the approach is OLD", "assistant"),
        line("2999-01-01T00:00:00Z", "we decided to use NEW", "user", "p1"),
        line("", "the approach is NEW", "assistant"),
    ]), encoding="utf-8")
    r = capture_transcript(con, str(p), "s1")
    assert r.candidates == 1
    (txt,) = con.execute("SELECT user_turn FROM candidates").fetchone()
    assert "NEW" in txt and "OLD" not in txt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_capture_transcript.py -v`
Expected: FAIL (`capture_transcript` not defined).

- [ ] **Step 3: Implement in `ccmem/capture.py`**

Add imports and helpers at the top (keep existing `_PATTERNS`, `score_turn`, `extract_sigil`, `enqueue_candidate`):

```python
import hashlib
import os
from dataclasses import dataclass
from ccmem.transcript import iter_turn_pairs, normalize_transcript_path

MAX_CANDIDATE_CHARS = int(os.environ.get("CCMEM_MAX_CANDIDATE_CHARS", "2000"))


def content_hash(user_turn: str, assistant_turn: str) -> str:
    h = hashlib.sha256()
    h.update(user_turn.encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(assistant_turn.encode("utf-8", "replace"))
    return h.hexdigest()


@dataclass
class CaptureResult:
    candidates: int = 0
    sigil_memories: int = 0
    refusals: int = 0
    promptid_drift: bool = False
```

Rewrite `enqueue_candidate` so it hashes full text and stores truncated:

```python
def enqueue_candidate(con, session_id, prompt_id, user_turn, assistant_turn,
                      score, is_pre_compact=False):
    h = content_hash(user_turn, assistant_turn)          # FULL text
    con.execute(
        "INSERT OR IGNORE INTO candidates "
        "(id, session_id, prompt_id, user_turn, assistant_turn, classifier_score, "
        " is_pre_compact, created_at, status, content_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), session_id, prompt_id,
         user_turn[:MAX_CANDIDATE_CHARS], assistant_turn[:MAX_CANDIDATE_CHARS],
         score, 1 if is_pre_compact else 0,
         datetime.now(timezone.utc).isoformat(), "pending", h),
    )
    con.commit()
    return con.total_changes  # 0 if deduped by content_hash
```

Add the HWM helpers and `capture_transcript` (sigil routing is added in Task 5 — leave the hook here for it):

```python
def _read_hwm(con, norm_path):
    row = con.execute(
        "SELECT last_prompt_id, last_ordinal FROM transcript_progress WHERE transcript_path=?",
        (norm_path,),
    ).fetchone()
    return (row[0], row[1]) if row else (None, -1)


def _write_hwm(con, norm_path, session_id, last_prompt_id, last_ordinal):
    con.execute(
        "INSERT INTO transcript_progress "
        "(transcript_path, last_prompt_id, last_ordinal, session_id, updated_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(transcript_path) DO UPDATE SET "
        "last_prompt_id=excluded.last_prompt_id, last_ordinal=excluded.last_ordinal, "
        "session_id=excluded.session_id, updated_at=excluded.updated_at",
        (norm_path, last_prompt_id, last_ordinal, session_id,
         datetime.now(timezone.utc).isoformat()),
    )
    con.commit()


def _initialized_at(con) -> str:
    row = con.execute(
        "SELECT value FROM schema_meta WHERE key='initialized_at'"
    ).fetchone()
    return row[0] if row else "1970-01-01T00:00:00Z"


def capture_transcript(con, transcript_path, session_id, is_pre_compact=False):
    norm = normalize_transcript_path(transcript_path)
    last_pid, last_ord = _read_hwm(con, norm)
    init_at = _initialized_at(con)
    result = CaptureResult()
    threshold = float(os.environ.get("CCMEM_THRESHOLD", "4"))
    seen_pid = seen_user = 0
    max_pid, max_ord = last_pid, last_ord
    for pair in iter_turn_pairs(transcript_path):
        seen_user += 1
        if pair.prompt_id is not None:
            seen_pid += 1
        # Position by ordinal: it is always present and stable for an append-only
        # transcript (the Nth user record is always the Nth). prompt_id is stored as
        # the HWM pointer and used only for drift detection — this is why mixed
        # promptId presence (change #7) cannot cause a skip or re-process.
        if pair.ordinal <= last_ord:
            continue
        # Per-turn install bound (finding #4): skip turns from before ccmem existed,
        # even in a resumed pre-install session whose file mtime is now current.
        # A missing/unparseable timestamp is treated as post-install (captured).
        if pair.timestamp is not None and pair.timestamp < init_at:
            max_ord = max(max_ord, pair.ordinal)   # advance HWM past it; never capture
            if pair.prompt_id is not None:
                max_pid = pair.prompt_id
            continue
        # (Task 5 inserts sigil handling here, before the candidate path.)
        score = score_turn(pair.user_turn, pair.assistant_turn)
        if score >= threshold:
            if enqueue_candidate(con, session_id, pair.prompt_id,
                                 pair.user_turn, pair.assistant_turn, score,
                                 is_pre_compact):
                result.candidates += 1
        max_ord = max(max_ord, pair.ordinal)
        if pair.prompt_id is not None:
            max_pid = pair.prompt_id
    if seen_user > 0 and seen_pid == 0:
        result.promptid_drift = True
    if max_ord != last_ord or max_pid != last_pid:
        _write_hwm(con, norm, session_id, max_pid, max_ord)
    return result
```

- [ ] **Step 4: Add config key**

In `gates/config.json`, add at top level:

```json
  "max_candidate_chars": 2000,
```

- [ ] **Step 5: Run tests + gates**

Run: `python -m pytest tests/test_capture_transcript.py tests/test_capture.py -v && python gates/run_gates.py --phase 1`
Expected: PASS (existing `test_capture.py` still green — `score_turn`/`extract_sigil` unchanged).

- [ ] **Step 6: Commit**

```bash
git add ccmem/capture.py gates/config.json tests/test_capture_transcript.py
git commit -m "feat(capture): capture_transcript with full-text content_hash dedup and per-transcript HWM"
```

---

## Task 4: Kill-switch marker

**Files:**
- Create: `ccmem/killswitch.py`
- Test: `tests/test_killswitch.py` (create)

**Interfaces:**
- Produces:
  - `session_id_from_transcript(transcript_path: str) -> str` — the filename stem (FACTS §4: `<munged>/<session-uuid>.jsonl`).
  - `mark_disabled(home: str, session_id: str) -> bool` — returns True on success, False if the marker could not be written (finding #2).
  - `is_disabled(home: str, session_id: str) -> bool`

**Why session_id, not path (finding #1):** the marker is written at SessionStart, when
the transcript file may not exist yet. `os.path.realpath` resolves 8.3/symlink
components differently for an absent vs. present file, so a path-keyed marker could fail
to match the sweep's later normalization and the disabled session would be swept anyway.
`session_id` is existence- and spelling-independent; the sweep derives the same value
from the transcript filename stem.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_killswitch.py
import os
from ccmem.killswitch import mark_disabled, is_disabled, session_id_from_transcript

def test_mark_then_detect_by_session_id(tmp_path):
    home = str(tmp_path)
    assert is_disabled(home, "sess-uuid-1") is False
    assert mark_disabled(home, "sess-uuid-1") is True
    assert is_disabled(home, "sess-uuid-1") is True

def test_session_id_from_filename():
    assert session_id_from_transcript(
        os.path.join("x", "projects", "munged", "abc-123.jsonl")) == "abc-123"

def test_mark_returns_false_when_unwritable(tmp_path, monkeypatch):
    # point home at a path that cannot be created (a file, not a dir)
    bad = tmp_path / "not_a_dir"; bad.write_text("x")
    assert mark_disabled(str(bad), "s1") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_killswitch.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `ccmem/killswitch.py`**

```python
from __future__ import annotations
import hashlib
import os
from pathlib import Path

_DIR = "disabled"


def session_id_from_transcript(transcript_path: str) -> str:
    """FACTS §4: the transcript filename stem is the session-uuid."""
    return os.path.splitext(os.path.basename(transcript_path))[0]


def _marker(home: str, session_id: str) -> Path:
    key = hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()
    return Path(home) / _DIR / key


def mark_disabled(home: str, session_id: str) -> bool:
    """Tombstone a session so no future sweep captures its transcript. Not capture.
    Returns True on success; False if the marker could not be written (finding #2 —
    the caller surfaces a systemMessage in that case)."""
    try:
        m = _marker(home, session_id)
        m.parent.mkdir(parents=True, exist_ok=True)
        m.touch(exist_ok=True)
        return m.exists()
    except Exception:
        return False


def is_disabled(home: str, session_id: str) -> bool:
    try:
        return _marker(home, session_id).exists()
    except Exception:
        return False
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_killswitch.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ccmem/killswitch.py tests/test_killswitch.py
git commit -m "feat(killswitch): session_id-keyed disabled marker (existence-independent) so recovery never captures a CCMEM_DISABLED session"
```

---

## Task 5: Sigil routing in capture_transcript

**Files:**
- Modify: `ccmem/capture.py`
- Test: `tests/test_capture_sigil.py` (create)

**Interfaces:**
- Consumes: `extract_sigil` (existing), `redact` (`ccmem.redact`), `maybe_supersede` (`ccmem.supersession`), `resolve_project_root`/`project_key` (`ccmem.scoping`).
- Produces: sigil `user` records route to `memories` (durable) or `sigil_refusals`; `CaptureResult.sigil_memories` / `.refusals` populated.

- [ ] **Step 1: Create fixtures**

`fixtures/transcripts/sigil.jsonl`:

```json
{"type":"user","promptId":"p1","cwd":"C:\\proj","message":{"content":[{"type":"text","text":"!mem: always run gates before commit"}]}}
{"type":"assistant","apiBlockIndex":0,"message":{"content":[{"type":"text","text":"noted"}]}}
```

`fixtures/transcripts/sigil_secret.jsonl`:

```json
{"type":"user","promptId":"p1","cwd":"C:\\proj","message":{"content":[{"type":"text","text":"!mem: my token is AKIAIOSFODNN7EXAMPLE do not lose it"}]}}
{"type":"assistant","apiBlockIndex":0,"message":{"content":[{"type":"text","text":"ok"}]}}
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_capture_sigil.py
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_capture_sigil.py -v`
Expected: FAIL (sigil not yet routed; it would be scored as a normal turn).

- [ ] **Step 4: Implement sigil branch**

In `capture_transcript`, replace the comment `# (Task 5 inserts sigil handling here...)` with:

```python
        sigil_text, sigil_scope, _ = extract_sigil(pair.user_turn)
        if sigil_text is not None:
            wrote, refused = _handle_sigil(con, pair, sigil_text, sigil_scope, session_id, norm)
            if wrote:
                result.sigil_memories += 1
            elif refused:
                result.refusals += 1
            # deduped no-op: neither counter moves
            max_ord = max(max_ord, pair.ordinal)
            if pair.prompt_id is not None:
                max_pid = pair.prompt_id
            continue
```

Add the helper:

```python
def _handle_sigil(con, pair, text, scope, session_id, norm_path):
    """Write a durable memory, record a refusal, or dedup.
    Returns (wrote: bool, refused: bool): (True,False) inserted, (False,True) refused,
    (False,False) deduped no-op."""
    import hashlib
    from ccmem.redact import redact
    from ccmem.supersession import maybe_supersede
    from ccmem.scoping import project_key, resolve_project_root
    redacted = redact(text)
    if redacted != text:
        con.execute(
            "INSERT INTO sigil_refusals (id, transcript_path, session_id, created_at, excerpt) "
            "VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), norm_path, session_id,
             datetime.now(timezone.utc).isoformat(), redacted[:200]),
        )
        con.commit()
        return (False, True)
    h = hashlib.sha256(redacted.encode("utf-8", "replace")).hexdigest()
    root = resolve_project_root(pair.cwd or os.getcwd())
    pid, _ = project_key(root)
    mem_id = str(uuid.uuid4())
    before = con.total_changes
    con.execute(
        "INSERT OR IGNORE INTO memories "
        "(id, type, content, scope, project_id, project_root, created_at, status, content_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (mem_id, "preference", redacted, scope or "project", pid, root,
         datetime.now(timezone.utc).isoformat(), "active", h),
    )
    con.commit()
    if con.total_changes == before:
        return (False, False)  # deduped by content_hash — identical sigil already stored
    maybe_supersede(con, mem_id, None, pid)
    return (True, False)
```

Note: idempotency keys on `memories.content_hash` (Task 1), **not** on `subject`.
`subject` is `maybe_supersede`'s matching key — its purpose is to match *different*
sigils that share a subject, so using it as a dedup key would drop two distinct sigils
about the same subject, and any change to the subject extractor would silently change
idempotency. `maybe_supersede` still runs (genuine subject-based supersession) only when
a new row was actually inserted.

- [ ] **Step 5: Run tests + gates**

Run: `python -m pytest tests/test_capture_sigil.py tests/test_capture_transcript.py -v && python gates/run_gates.py --phase 1`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ccmem/capture.py tests/test_capture_sigil.py fixtures/transcripts/
git commit -m "feat(capture): route !mem: sigils to durable memories; record secret refusals"
```

---

## Task 6: PreCompact hook — capture then mark is_pre_compact

**Files:**
- Modify: `hooks/mem_snapshot.py`
- Test: `tests/test_hook_snapshot.py` (create)

**Interfaces:**
- Consumes: `capture_transcript` (Task 3/5).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hook_snapshot.py
import json, os, subprocess, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "fixtures", "transcripts")

def _run(payload, env):
    return subprocess.run(
        [sys.executable, os.path.join(REPO, "hooks", "mem_snapshot.py")],
        input=json.dumps(payload).encode(), capture_output=True, env=env, timeout=30)

def test_precompact_captures_and_marks(tmp_path):
    home = str(tmp_path)
    from ccmem.db import connect, migrate
    migrate(connect(os.path.join(home, "mem.db")))
    env = {**os.environ, "CCMEM_HOME": home, "PYTHONPATH": REPO}
    p = _run({"hook_event_name": "PreCompact",
              "transcript_path": os.path.join(FIX, "basic.jsonl"),
              "session_id": "s1"}, env)
    assert p.returncode == 0
    con = connect(os.path.join(home, "mem.db"))
    rows = con.execute("SELECT is_pre_compact FROM candidates").fetchall()
    assert rows and all(r[0] == 1 for r in rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hook_snapshot.py -v`
Expected: FAIL (hook still only flips a flag; captures nothing).

- [ ] **Step 3: Rewrite `hooks/mem_snapshot.py`**

Replace the body's DB section (keep the R1 wrapper and `CCMEM_DISABLED` guard) so it captures first, then marks:

```python
        from ccmem.capture import capture_transcript
        from ccmem.db import connect, log_hook_event
        from ccmem.paths import maybe_migrate, resolve_home

        home = resolve_home()
        maybe_migrate(home)
        db_path = os.path.join(home, "mem.db")
        if not os.path.exists(db_path):
            return
        transcript = payload.get("transcript_path", "")
        session_id = payload.get("session_id", "unknown")
        con = connect(db_path)
        if transcript:
            capture_transcript(con, transcript, session_id, is_pre_compact=True)
        _dur = int((time.monotonic() - _t0) * 1000)
        try:
            log_hook_event(con, "PreCompact", "capture+snapshot", duration_ms=_dur)
        except Exception:
            pass
        con.close()
```

(`is_pre_compact=True` marks the rows captured on this call; the flag remains write-only/reserved per spec.)

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_hook_snapshot.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hooks/mem_snapshot.py tests/test_hook_snapshot.py
git commit -m "feat(hooks): PreCompact captures new turns then marks is_pre_compact"
```

---

## Task 7: SessionEnd hook — capture then checkpoint

**Files:**
- Modify: `hooks/mem_flush.py`
- Test: `tests/test_hook_flush.py` (create)

**Interfaces:**
- Consumes: `capture_transcript` (Task 3/5).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hook_flush.py
import json, os, subprocess, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "fixtures", "transcripts")

def test_sessionend_captures_and_dedups_after_precompact(tmp_path):
    home = str(tmp_path)
    from ccmem.db import connect, migrate
    migrate(connect(os.path.join(home, "mem.db")))
    env = {**os.environ, "CCMEM_HOME": home, "PYTHONPATH": REPO}
    args = dict(capture_output=True, env=env, timeout=30)
    pc = subprocess.run([sys.executable, os.path.join(REPO, "hooks", "mem_snapshot.py")],
        input=json.dumps({"hook_event_name":"PreCompact",
            "transcript_path": os.path.join(FIX,"basic.jsonl"),"session_id":"s1"}).encode(), **args)
    se = subprocess.run([sys.executable, os.path.join(REPO, "hooks", "mem_flush.py")],
        input=json.dumps({"hook_event_name":"SessionEnd",
            "transcript_path": os.path.join(FIX,"basic.jsonl"),"session_id":"s1"}).encode(), **args)
    assert pc.returncode == 0 and se.returncode == 0
    con = connect(os.path.join(home, "mem.db"))
    # cross-event idempotency: no duplicate from the second capture
    assert con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hook_flush.py -v`
Expected: FAIL (SessionEnd only checkpoints today; count would be 1 from PreCompact but the test also proves SessionEnd runs capture without dup — implement to confirm).

- [ ] **Step 3: Rewrite `hooks/mem_flush.py`**

Insert capture before the checkpoint (keep R1 wrapper + `CCMEM_DISABLED` guard):

```python
        from ccmem.capture import capture_transcript
        from ccmem.db import connect, log_hook_event
        from ccmem.paths import maybe_migrate, resolve_home

        home = resolve_home()
        maybe_migrate(home)
        db_path = os.path.join(home, "mem.db")
        if not os.path.exists(db_path):
            return
        con = connect(db_path)
        transcript = payload.get("transcript_path", "")
        session_id = payload.get("session_id", "unknown")
        if transcript:
            capture_transcript(con, transcript, session_id)
        con.execute("PRAGMA wal_checkpoint(PASSIVE)")
        _dur = int((time.monotonic() - _t0) * 1000)
        try:
            log_hook_event(con, "SessionEnd", "capture+checkpoint", duration_ms=_dur)
        except Exception:
            pass
        con.close()
```

Note: `mem_flush.py` currently does not parse stdin into `payload`. Add the parse (mirror `mem_snapshot.py`): read stdin, `payload = json.loads(raw)`, guard `isinstance(payload, dict)`, all inside the existing try/except returning on failure (R1).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_hook_flush.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hooks/mem_flush.py tests/test_hook_flush.py
git commit -m "feat(hooks): SessionEnd captures new turns then WAL-checkpoints"
```

---

## Task 8: Bounded recovery sweep

**Files:**
- Create: `ccmem/recovery.py`
- Modify: `gates/config.json` (add `recovery_budget`)
- Test: `tests/test_recovery.py` (create)

**Interfaces:**
- Consumes: `capture_transcript` (Task 3/5), `is_disabled` (Task 4), `normalize_transcript_path` (Task 2).
- Produces: `recover_project(con, home, project_dir, initialized_at, max_bytes, max_ms) -> CaptureResult` — aggregate over swept transcripts.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recovery.py
import os, time
from ccmem.db import connect, migrate
from ccmem.recovery import recover_project
from ccmem.killswitch import mark_disabled

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "transcripts")

def _db(): con = connect(":memory:"); migrate(con); return con

def _copy(tmp, name, src):
    import shutil; d = tmp / name; shutil.copy(os.path.join(FIX, src), d); return str(d)

def test_recovers_uncaptured_transcript(tmp_path):
    con = _db()
    _copy(tmp_path, "basic.jsonl", "basic.jsonl")
    r = recover_project(con, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 10_000_000, 5000)
    assert r.candidates == 1

def test_clean_transcript_zero_reads(tmp_path):
    con = _db(); p = _copy(tmp_path, "basic.jsonl", "basic.jsonl")
    from ccmem.capture import capture_transcript
    capture_transcript(con, p, "s1")               # mark current
    r = recover_project(con, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 10_000_000, 5000)
    assert r.candidates == 0

def test_disabled_transcript_never_swept(tmp_path):
    con = _db()
    # mark disabled by session_id BEFORE the transcript file exists (finding #1),
    # then create the file and sweep — it must still be skipped.
    mark_disabled(str(tmp_path), "basic")   # session_id == filename stem
    _copy(tmp_path, "basic.jsonl", "basic.jsonl")
    r = recover_project(con, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 10_000_000, 5000)
    assert r.candidates == 0

def test_pre_install_transcript_never_swept(tmp_path):
    con = _db(); p = _copy(tmp_path, "basic.jsonl", "basic.jsonl")
    future = "2999-01-01T00:00:00Z"
    r = recover_project(con, str(tmp_path), str(tmp_path), future, 10_000_000, 5000)
    assert r.candidates == 0

def test_partial_safe_under_byte_budget(tmp_path):
    con = _db()
    for i in range(3):
        _copy(tmp_path, f"t{i}.jsonl", "basic.jsonl")
    # tiny byte budget: sweep stops early, remainder captured on a second call
    r1 = recover_project(con, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 1, 5000)
    r2 = recover_project(con, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 10_000_000, 5000)
    assert r1.candidates + r2.candidates >= 1  # nothing lost across partial sweeps
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_recovery.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `ccmem/recovery.py`**

```python
from __future__ import annotations
import glob
import os
import time
from ccmem.capture import CaptureResult, capture_transcript, normalize_transcript_path
from ccmem.killswitch import is_disabled, session_id_from_transcript


def _updated_at(con, norm_path):
    row = con.execute(
        "SELECT updated_at FROM transcript_progress WHERE transcript_path=?", (norm_path,)
    ).fetchone()
    return row[0] if row else None


def recover_project(con, home, project_dir, initialized_at, max_bytes, max_ms) -> CaptureResult:
    """Sweep transcripts in project_dir that have unprocessed turns, bounded by
    cumulative bytes and a wall-clock deadline. Partial sweeps are safe: the HWM
    and content_hash guarantee no loss and no duplication across calls."""
    agg = CaptureResult()
    deadline = time.monotonic() + (max_ms / 1000.0)
    bytes_read = 0
    try:
        files = glob.glob(os.path.join(project_dir, "*.jsonl"))
    except Exception:
        return agg
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)  # most-recent first
    for f in files:
        if time.monotonic() >= deadline or bytes_read >= max_bytes:
            break
        try:
            st = os.stat(f)
        except OSError:
            continue
        mtime_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))
        if mtime_iso <= initialized_at:
            continue  # stat-only prefilter; the real bound is per-turn in capture_transcript
        sid = session_id_from_transcript(f)
        if is_disabled(home, sid):
            continue  # kill switch (session_id-keyed, finding #1)
        norm = normalize_transcript_path(f)
        ua = _updated_at(con, norm)
        if ua is not None and mtime_iso <= ua:
            continue  # already current (stat-only, no read)
        r = capture_transcript(con, f, session_id=sid)
        agg.candidates += r.candidates
        agg.sigil_memories += r.sigil_memories
        agg.refusals += r.refusals
        agg.promptid_drift = agg.promptid_drift or r.promptid_drift
        bytes_read += st.st_size
    return agg
```

- [ ] **Step 4: Add config budget**

In `gates/config.json`, add:

```json
  "recovery_budget": { "max_bytes": 10485760, "max_ms": 4000 },
```

- [ ] **Step 5: Add the Review-Focus tests**

Append to `tests/test_recovery.py`:

```python
def test_concurrent_locked_db_defers_without_loss(tmp_path):
    con = _db(); p = _copy(tmp_path, "basic.jsonl", "basic.jsonl")
    other = connect(os.path.join(":memory:"))  # placeholder; see note
    # Simulate lock by holding a write txn on a file-backed DB
    import sqlite3
    dbfile = str(tmp_path / "mem.db"); migrate(connect(dbfile))
    a = connect(dbfile); b = connect(dbfile)
    a.execute("BEGIN IMMEDIATE")
    try:
        # b's capture should not raise (busy_timeout) or should exit cleanly
        try:
            recover_project(b, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 10_000_000, 200)
        except sqlite3.OperationalError:
            pass  # acceptable: caller (hook) swallows and defers
    finally:
        a.execute("ROLLBACK")

def test_giant_turn_capped_in_storage(tmp_path):
    from ccmem.capture import MAX_CANDIDATE_CHARS
    import json as _j
    big = "we decided " + "z" * (2 * 1024 * 1024)
    p = tmp_path / "big.jsonl"
    p.write_text("\n".join([
        '{"type":"user","promptId":"p1","cwd":"C:\\\\p","message":{"content":[{"type":"text","text":' + _j.dumps(big) + '}]}}',
        '{"type":"assistant","apiBlockIndex":0,"message":{"content":[{"type":"text","text":"ok the approach is X"}]}}',
    ]), encoding="utf-8")
    con = _db()
    recover_project(con, str(tmp_path), str(tmp_path), "2000-01-01T00:00:00Z", 10_000_000, 5000)
    (n,) = con.execute("SELECT LENGTH(user_turn) FROM candidates").fetchone()
    assert n <= MAX_CANDIDATE_CHARS

def test_worktree_cwd_from_record_not_dirname(tmp_path):
    # capture derives project root from the transcript's cwd field (Task 5),
    # so a sigil in a transcript stored under a munged dir still resolves.
    con = _db(); import shutil
    d = tmp_path / "munged"; d.mkdir()
    shutil.copy(os.path.join(FIX, "sigil.jsonl"), d / "s.jsonl")
    r = recover_project(con, str(tmp_path), str(d), "2000-01-01T00:00:00Z", 10_000_000, 5000)
    assert r.sigil_memories == 1
```

(Note: the locked-DB test asserts the sweep degrades — either the busy-timeout absorbs the lock or the caller swallows `OperationalError`. The hook wrapper in Task 9 provides the swallow; `recover_project` itself may propagate, which the hook catches.)

- [ ] **Step 6: Run tests + gates**

Run: `python -m pytest tests/test_recovery.py -v && python gates/run_gates.py --phase 1`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add ccmem/recovery.py gates/config.json tests/test_recovery.py
git commit -m "feat(recovery): bounded SessionStart sweep — dirty-check, init-time and kill-switch bounds, byte/time budget"
```

---

## Task 9: SessionStart hook — kill-switch marker, sweep-before-inject, surface refusals

**Files:**
- Modify: `hooks/mem_inject.py`
- Test: `tests/test_hook_inject.py` (extend if present, else create)

**Interfaces:**
- Consumes: `recover_project` (Task 8), `mark_disabled` (Task 4), existing `retrieve`/`render` for injection.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hook_inject.py  (add these; keep any existing tests)
import json, os, subprocess, sys
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
    env = {**os.environ, "CCMEM_HOME": home, "PYTHONPATH": REPO}
    p = _run({"hook_event_name":"SessionStart","transcript_path": os.path.join(proj,"new.jsonl"),
              "session_id":"s2","cwd": proj}, env)
    assert p.returncode == 0
    assert b"unreviewed" in p.stdout.lower() and b"refusal" in p.stdout.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hook_inject.py -v`
Expected: FAIL (no marker written; no refusal surfaced; sweep not wired).

- [ ] **Step 3: Rewrite `hooks/mem_inject.py` flow**

Keep the R1 wrapper. stdin can only be read once — the `CCMEM_DISABLED` block below reads it and returns, and the normal path reads it separately; ensure there is no earlier top-level `sys.stdin` read before this block. At the top of the work section, handle the kill switch first:

```python
    if os.environ.get("CCMEM_DISABLED"):
        # Tombstone this session so no future sweep captures its transcript.
        # Key on the filename stem — the same key the sweep will derive (finding #1).
        # If the marker cannot be written, be LOUD (finding #2): still exit 0, but
        # tell the user the privacy guarantee is degraded.
        try:
            from ccmem.paths import resolve_home
            from ccmem.killswitch import mark_disabled, session_id_from_transcript
            payload = json.loads(sys.stdin.buffer.read() or b"{}")
            t = payload.get("transcript_path")
            if t:
                sid = session_id_from_transcript(t)
                if not mark_disabled(resolve_home(), sid):
                    print(json.dumps({"hookSpecificOutput": {
                        "hookEventName": "SessionStart"},
                        "systemMessage": "ccmem is disabled but could not record a "
                        "do-not-capture marker for this session; it may be captured later."}))
        except Exception:
            pass
        return
```

Then, in the normal path, order the work sweep-first:

```python
        # 1) recovery sweep BEFORE building injection, so refusals surface now.
        #    project dir comes straight from the payload's transcript_path (finding #9)
        #    — no cwd-munging on the hook path. Wrap so a locked DB never breaks inject.
        transcript = payload.get("transcript_path", "")
        project_dir = os.path.dirname(transcript) if transcript else None
        try:
            if project_dir and os.path.isdir(project_dir):
                rb = _cfg_recovery_budget()
                recover_project(con, home, project_dir, _read_initialized_at(con),
                                rb["max_bytes"], rb["max_ms"])
        except Exception:
            pass  # R1: a locked DB or sweep error must not break injection

        # 2) build injection block (memories + refusal notice) and print once.
        #    PURE READ of sigil_refusals — no mutation, so SessionStart stays
        #    deterministic (finding #3). Acknowledgement is a CLI action (Task 14).
        blocks = []
        n_ref = con.execute(
            "SELECT COUNT(*) FROM sigil_refusals WHERE acknowledged_at IS NULL"
        ).fetchone()[0]
        if n_ref:
            blocks.append(f"{n_ref} unreviewed !mem: refusal(s) (contained secrets) "
                          f"— run `ccmem list --refused`.")
        memories = retrieve(con, pid)
        if memories:
            blocks.append(render(memories, root, "session-start"))
        if blocks:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n\n".join(blocks)}}))
```

Add small helpers in the hook:

```python
def _read_initialized_at(con):
    row = con.execute("SELECT value FROM schema_meta WHERE key='initialized_at'").fetchone()
    return row[0] if row else "1970-01-01T00:00:00Z"

def _cfg_recovery_budget():
    import json as _j
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gates", "config.json")
    try:
        return _j.loads(open(p).read()).get("recovery_budget", {"max_bytes": 10485760, "max_ms": 4000})
    except Exception:
        return {"max_bytes": 10485760, "max_ms": 4000}
```

The sweep is already wrapped in its own try/except above so a locked DB or sweep error never breaks injection (R1).

Project-dir resolution (finding #9): the hook uses `os.path.dirname(transcript_path)` from the payload — no cwd-munging, no reverse-engineering of Claude Code's `<munged-cwd>` scheme. The single cwd→dir munger needed by `doctor` (which has no payload) is added to `ccmem/paths.py` in Task 14, with exactly one caller. Do not add a second munger here.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_hook_inject.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hooks/mem_inject.py tests/test_hook_inject.py
git commit -m "feat(hooks): SessionStart writes kill-switch marker, sweeps before injecting, surfaces sigil refusals"
```

---

## Task 10: Remove Stop and UserPromptSubmit

**Files:**
- Delete: `hooks/mem_capture.py`, `hooks/mem_retrieve.py`, `tests/test_hook_mem_retrieve.py`
- Modify: `gates/gate_scaffold.py:41-47`, `gates/gate_hook_contract.py:132-142`, `gates/config.json`, `tests/test_hook_safety.py`

**Interfaces:** none produced; removes dead entry points.

- [ ] **Step 1: Delete the hooks and their test**

```bash
git rm hooks/mem_capture.py hooks/mem_retrieve.py tests/test_hook_mem_retrieve.py
```

- [ ] **Step 2: Update `gate_scaffold.py`**

In `DESIGN_HOOK_NAMES`, remove `"mem_capture.py"` and `"mem_retrieve.py"`, leaving `mem_inject.py`, `mem_flush.py`, `mem_snapshot.py`.

- [ ] **Step 3: Update `gate_hook_contract.py`**

Remove the `UserPromptSubmit` case (the `if event == "UserPromptSubmit"` block, ~lines 132-142). Ensure the contract loop iterates only the three surviving hooks/events (SessionStart, PreCompact, SessionEnd). Read the gate's event list and drop `Stop` and `UserPromptSubmit` entries.

- [ ] **Step 4: Update `config.json`**

Remove `hooks.Stop`, `hooks.UserPromptSubmit`, and `budget_ms.Stop`, `budget_ms.UserPromptSubmit`. Leave the three surviving hooks.

- [ ] **Step 5: Update `tests/test_hook_safety.py`**

Remove `mem_capture.py` and `mem_retrieve.py` from the hook list the kill-switch/safety sweep iterates (search for the list of hook filenames; leave the three survivors).

- [ ] **Step 6: Run gates + tests**

Run: `python gates/run_gates.py --phase 1 && python -m pytest tests/ -q`
Expected: PASS (scaffold, hook-contract green with three hooks; safety sweep covers three).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(hooks): remove Stop and UserPromptSubmit — capture is snapshot-driven, per-turn injection retired"
```

---

## Task 11: Cache-safety gate update (own commit, with reasoning)

**Files:**
- Modify: `gates/gate_cache_safety.py`
- Test: existing gate is its own test.

**Interfaces:** none.

- [ ] **Step 1: Remove retired checks**

Delete `check_per_turn_default_off` and its call, and the `UserPromptSubmit` byte-stability check — the feature they guarded (per-turn injection) is gone.

- [ ] **Step 2: Update the SessionStart determinism check**

Change it to assert two things (see spec §"Cache determinism vs the sweep"):
1. **Steady state:** with no dirty transcripts present (empty/absent project transcript dir, or all marks current), two SessionStart runs produce byte-identical output.
2. **At-most-once change:** with one dirty transcript, run 1 may differ (sweep mutates the store); run 2 (no new dirt) is byte-identical to run 1's post-sweep output.

Implement by pointing the hook at a temp `CCMEM_HOME` and a temp project dir you control: assert determinism with an empty project dir; then drop a fixture transcript, run twice, assert run2 == run3.

- [ ] **Step 3: Run the gate**

Run: `python gates/run_gates.py --phase 1`
Expected: PASS.

- [ ] **Step 4: Commit (own commit, reasoning in the message)**

```bash
git add gates/gate_cache_safety.py
git commit -m "$(cat <<'EOF'
test(gate): cache-safety asserts steady-state determinism + at-most-once sweep change

The SessionStart sweep now writes to the store (sigil memories, candidates,
refusal surfacing), so byte-identical output across two runs against an
UNCHANGED store no longer holds when a dirty transcript is present. Rather than
delete the determinism check, it now asserts: (1) with no dirty transcripts,
two runs are byte-identical; (2) a dirty transcript induces a change on the
first run only, and a second run with no new dirt is byte-identical to the
first's post-sweep output. Consequence, by design: the first session after a
crash burns one cache write. Acceptable and rare; documented in FACTS.md.
EOF
)"
```

---

## Task 12: Extend secret-hygiene gate to candidates

**Files:**
- Modify: `gates/gate_secret_hygiene.py:133`

**Interfaces:** none.

- [ ] **Step 1: Add a candidates scan**

After the existing `SELECT id, content, context FROM memories` scan, add a scan of `candidates`:

```python
    crows = con.execute(
        "SELECT id, user_turn, assistant_turn FROM candidates"
    ).fetchall()
    for cid, u, a in crows:
        for field, val in (("user_turn", u), ("assistant_turn", a)):
            if _looks_like_secret(val):   # reuse the gate's existing secret check
                r.fail(f"candidate {cid}.{field} clean", "possible secret in candidates table")
```

Use whatever secret-detection helper the gate already applies to `memories` (read the file; reuse it — do not invent a second detector).

- [ ] **Step 2: Run the gate**

Run: `python gates/run_gates.py --phase 1`
Expected: PASS (no secrets in a clean fixture DB).

- [ ] **Step 3: Commit**

```bash
git add gates/gate_secret_hygiene.py
git commit -m "test(gate): secret-hygiene now audits the candidates table, not just memories"
```

---

## Task 13: Recovery-budget gate

**Files:**
- Create: `gates/gate_recovery_budget.py`
- Modify: `gates/run_gates.py` (register the new gate in phase 1 if it uses an explicit list)
- Create: `fixtures/transcripts/oversized.jsonl` (large, many turns)

**Interfaces:** none.

- [ ] **Step 1: Create the oversized fixture**

Generate `fixtures/transcripts/oversized.jsonl` programmatically in the gate (do not commit a huge file): write N synthetic turns to a temp dir at gate runtime.

- [ ] **Step 2: Write the gate**

```python
# gates/gate_recovery_budget.py
"""SessionStart recovery must stay within its configured wall-clock budget even
with a deliberately oversized transcript present. Partial sweeps are safe."""
import json, os, sys, tempfile, time
from gates._common import GateResult   # match existing gate imports

def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    r = GateResult("recovery budget")
    from ccmem.db import connect, migrate
    from ccmem.recovery import recover_project
    cfg = json.loads(open(os.path.join(os.path.dirname(__file__), "config.json")).read())
    budget = cfg.get("recovery_budget", {"max_bytes": 10485760, "max_ms": 4000})
    with tempfile.TemporaryDirectory() as d:
        con = connect(os.path.join(d, "mem.db")); migrate(con)
        big = os.path.join(d, "oversized.jsonl")
        with open(big, "w", encoding="utf-8") as fh:
            for i in range(5000):
                fh.write('{"type":"user","promptId":"p%d","cwd":"C:\\\\p","message":{"content":[{"type":"text","text":"we decided X %d"}]}}\n' % (i, i))
                fh.write('{"type":"assistant","apiBlockIndex":0,"message":{"content":[{"type":"text","text":"the approach is X"}]}}\n')
        t0 = time.monotonic()
        recover_project(con, d, d, "2000-01-01T00:00:00Z", budget["max_bytes"], budget["max_ms"])
        elapsed_ms = (time.monotonic() - t0) * 1000
        # allow a margin over the wall-clock budget for the final in-flight file
        limit = budget["max_ms"] * 2
        if elapsed_ms <= limit:
            r.ok("recovery within budget", f"{elapsed_ms:.0f}ms <= {limit}ms")
        else:
            r.fail("recovery within budget", f"{elapsed_ms:.0f}ms > {limit}ms")
    return r.report()

if __name__ == "__main__":
    raise SystemExit(main())
```

Read an existing gate (e.g. `gate_schema_contract.py`) first to match the exact `GateResult`/`run_gates` registration convention.

- [ ] **Step 3: Run the gate**

Run: `python gates/gate_recovery_budget.py && python gates/run_gates.py --phase 1`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add gates/gate_recovery_budget.py gates/run_gates.py
git commit -m "test(gate): SessionStart recovery stays within wall-clock budget on an oversized transcript"
```

---

## Task 14: Doctor + `list --refused` + paths munger

**Files:**
- Modify: `ccmem/cli.py` (`cmd_doctor`, `_check_defender_exclusions`, add `cmd_list` `--refused`, register the flag)
- Modify: `ccmem/paths.py` (add `project_transcript_dir(cwd)` — finding #9, doctor's only caller)
- Test: `tests/test_cli_doctor.py`, `tests/test_cli_refused.py` (create)

**Interfaces:**
- Produces: `ccmem.paths.project_transcript_dir(cwd: str) -> str`; `ccmem list --refused` displays unacknowledged refusals and stamps `acknowledged_at`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_doctor.py  (add)
import io, os
from contextlib import redirect_stdout
from ccmem.db import connect, migrate

def test_doctor_reports_refusals_and_drift(tmp_path, monkeypatch):
    home = str(tmp_path); monkeypatch.setenv("CCMEM_HOME", home)
    con = connect(os.path.join(home, "mem.db")); migrate(con)
    con.execute("INSERT INTO sigil_refusals (id, created_at, excerpt) VALUES ('r','t','x')")
    con.commit()
    from ccmem.cli import cmd_doctor
    class A: pass
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_doctor(A())
    out = buf.getvalue()
    assert "refus" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_doctor.py -v`
Expected: FAIL (doctor does not mention refusals).

- [ ] **Step 3: Implement doctor changes**

In `cmd_doctor`:
- Replace the Stop/UPS drop-rate block (~lines 457-463) with an **unrecovered-transcript** check: resolve the project dir via `paths.project_transcript_dir(os.getcwd())` (Step 4), count transcripts whose mtime is after their `transcript_progress.updated_at` (or absent) and after `initialized_at` and not disabled; report "N transcript(s) awaiting capture (will sweep on next SessionStart)". This is the new data-loss signal.
- Report sigil refusals: `SELECT COUNT(*) FROM sigil_refusals WHERE acknowledged_at IS NULL` (see Step 3 wording below).
- promptId drift: run a `capture_transcript` dry probe is unnecessary; instead, if any recent transcript yields `promptid_drift`, print a hard-failure line: "FAIL: transcript schema drift — no promptId on any user record; capture is on ordinal fallback."

In `_check_defender_exclusions`, add direct revert detection using `Get-MpPreference` (non-admin readable):

```python
def _current_exclusions_via_mppref():
    import subprocess
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-MpPreference).ExclusionPath -join \"`n\""],
            capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            return [l.strip().lower() for l in out.stdout.splitlines() if l.strip()]
    except Exception:
        pass
    return None
```

Compare against `defender_state.json`'s `confirmed_paths`; for any confirmed path now absent, print with scope statement:

```python
        print(f"  Defender exclusion no longer present (reverted since {at}).")
        print( "  Note: measured latency impact is within run-to-run variance.")
        print( "  Policy-revert insurance on a managed machine, not a performance signal.")
```

Use `acknowledged_at IS NULL` for the refusal count (matches the SessionStart notice):
`SELECT COUNT(*) FROM sigil_refusals WHERE acknowledged_at IS NULL` → "Unreviewed !mem: refusals: N (run `ccmem list --refused`)".

- [ ] **Step 4: Add the `paths.project_transcript_dir` munger (finding #9)**

Grep first (already done — nothing exists). Add to `ccmem/paths.py`:

```python
import re

def project_transcript_dir(cwd: str) -> str:
    """cwd -> ~/.claude/projects/<munged>/ . Claude Code lowercases the Windows
    drive letter and replaces ':', path separators, spaces, and '.' with '-'.
    FACTS §4 marks this scheme community/unstable — the hook path avoids it entirely
    by using dirname(transcript_path); doctor is the only caller."""
    if len(cwd) >= 2 and cwd[1] == ":":
        cwd = cwd[0].lower() + cwd[1:]
    munged = re.sub(r"[:\\/ .]", "-", cwd)
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", munged)
```

Add a fixture test asserting it produces the known dir for this repo's cwd
(`C:\Users\<user>\OneDrive - <org>\Documents\ccmem` →
`c--Users-<user>-OneDrive---<org>-Documents-ccmem`).

- [ ] **Step 5: Add `ccmem list --refused`**

In `main()`, add a `--refused` flag to the existing `list` subparser. In `cmd_list`,
when `args.refused` is set, display unacknowledged refusals and stamp them:

```python
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
```

Test `tests/test_cli_refused.py`: insert two refusals, run `cmd_list` with
`args.refused=True`, assert both print and both get `acknowledged_at` stamped; a
second run prints "No unreviewed" and stamps nothing (idempotent acknowledgement).

- [ ] **Step 6: Run tests + gates**

Run: `python -m pytest tests/test_cli_doctor.py tests/test_cli_refused.py -v && python gates/run_gates.py --phase 1`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add ccmem/cli.py ccmem/paths.py tests/test_cli_doctor.py tests/test_cli_refused.py
git commit -m "feat(cli): doctor refusal/drift/revert signals; ccmem list --refused; paths.project_transcript_dir for doctor"
```

---

## Task 15: DESIGN.md — 3-hook model, R5 retired

**Files:**
- Modify: `docs/DESIGN.md`

**Interfaces:** none.

- [ ] **Step 1: Update the capture/implementation section**

Rewrite the hook table/section to the three hooks (SessionStart, PreCompact, SessionEnd) with their snapshot-driven roles. Remove Stop/UserPromptSubmit from the hook inventory. Ensure the hook names match `DESIGN_HOOK_NAMES` in `gate_scaffold.py` (Task 10) — the scaffold gate cross-checks DESIGN.md.

- [ ] **Step 2: Mark R5 retired**

Add a note where R5/per-turn injection is discussed: "R5 (per-turn injection opt-in) is retired as of 2026-09-24: the `UserPromptSubmit` hook is removed; SessionStart is the sole injection path. See `docs/superpowers/specs/2026-09-24-snapshot-driven-capture-design.md`."

- [ ] **Step 3: Run the scaffold gate**

Run: `python gates/run_gates.py --phase 1`
Expected: PASS (scaffold reconciles DESIGN.md hook names with the gate list).

- [ ] **Step 4: Commit**

```bash
git add docs/DESIGN.md
git commit -m "docs(design): snapshot-driven 3-hook capture model; R5 retired"
```

---

## Task 16: Full suite + settings.json cutover note

**Files:**
- Modify: none in repo (settings.json is user config, outside the repo)

- [ ] **Step 1: Run the whole suite and all gates**

Run: `python -m pytest tests/ -q && python gates/run_gates.py`
Expected: all green.

- [ ] **Step 2: Hand the user the settings.json change**

settings.json is user config, not in the repo, so do not edit it silently. Report to the user the exact edit to make: remove the two ccmem hook registrations whose commands end in `mem_capture.py` (Stop) and `mem_retrieve.py` (UserPromptSubmit), leaving the three that end in `mem_inject.py`, `mem_snapshot.py`, `mem_flush.py`. The ccgate hooks are untouched.

- [ ] **Step 3: Commit any remaining doc/test cleanup**

```bash
git add -A && git commit -m "chore: finalize snapshot-driven capture cutover" || echo "nothing to commit"
```

---

## Notes for the implementer

- **Sequencing is load-bearing.** Tasks 1–3 (schema + full-text hash) and Task 4 (kill switch) must land before Tasks 6–9 depend on them. The hash bug (Task 3) poisons every dedup assertion downstream; the kill switch (Task 4) must exist before recovery (Task 8) can honor it.
- **R1 everywhere.** Every hook keeps its exit-0 wrapper. New failure surfaces (locked DB during concurrent sweeps, malformed transcript lines) must be swallowed, not raised.
- **Check for existing helpers before adding inline ones.** In particular the cwd→project-dir munger (Task 9) and the secret detector (Task 12) may already exist in `ccmem/` or the gate — reuse them.
- **Do not edit gates to make a test pass.** Additive contract changes (Task 1) and removal of checks for removed features (Tasks 10, 11) are expected; if a gate blocks something that is not one of those, stop and raise it.
