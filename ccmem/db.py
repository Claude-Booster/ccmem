from __future__ import annotations
import sqlite3
from pathlib import Path

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    content      TEXT NOT NULL,
    context      TEXT,
    subject      TEXT,
    scope        TEXT NOT NULL DEFAULT 'project',
    project_id   TEXT NOT NULL,
    project_root TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    accessed_at  TEXT,
    access_count INTEGER DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'active',
    supersedes   TEXT REFERENCES memories(id),
    embedding    BLOB,
    content_hash TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    prompt_id        TEXT,
    user_turn        TEXT NOT NULL,
    assistant_turn   TEXT NOT NULL,
    classifier_score REAL NOT NULL,
    is_pre_compact   INTEGER DEFAULT 0,
    created_at       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    content_hash     TEXT
);

CREATE TABLE IF NOT EXISTS session_injections (
    session_id  TEXT NOT NULL,
    memory_id   TEXT REFERENCES memories(id),
    injected_at TEXT NOT NULL,
    PRIMARY KEY (session_id, memory_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, subject, context,
    content=memories, content_rowid=rowid
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hook_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event       TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    excerpt     TEXT NOT NULL,
    duration_ms INTEGER
);

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

INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', '2');
INSERT OR IGNORE INTO schema_meta VALUES ('embedding_dim', '384');
INSERT OR IGNORE INTO schema_meta VALUES ('initialized_at', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
"""


def connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=3000")
    return con


def migrate(con: sqlite3.Connection) -> None:
    con.executescript(_DDL)
    # Idempotent column additions for DBs created before hook_log schema change.
    try:
        con.execute("ALTER TABLE hook_log ADD COLUMN duration_ms INTEGER")
        con.commit()
    except Exception:
        pass  # column already exists
    # Idempotent column additions for DBs created before schema_version 2.
    for tbl in ("candidates", "memories"):
        try:
            con.execute(f"ALTER TABLE {tbl} ADD COLUMN content_hash TEXT")
            con.commit()
        except Exception:
            pass  # column already exists
    # Unique indexes on content_hash — created after ALTER TABLE loop so the
    # column is guaranteed to exist on both fresh and pre-v2 DBs.
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_content_hash"
        " ON candidates(content_hash)"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash"
        " ON memories(content_hash)"
    )
    con.commit()


def log_hook_event(
    con: sqlite3.Connection,
    event: str,
    excerpt: str,
    duration_ms: int | None = None,
    keep: int = 10,
) -> None:
    """Append one row to hook_log and trim the table to the last `keep` rows.

    `recorded_at` uses datetime('now') inside SQLite so the value is produced
    by the DB engine, not Python — this keeps the call site cache-safe.
    `duration_ms` is the wall-clock time for the full hook invocation measured
    with time.monotonic() at the hook entry/exit; it never reaches injected
    output so it does not need to be cache-safe.
    """
    con.execute(
        "INSERT INTO hook_log (event, recorded_at, excerpt, duration_ms) "
        "VALUES (?, datetime('now'), ?, ?)",
        (event, excerpt[:200], duration_ms),
    )
    con.execute(
        "DELETE FROM hook_log WHERE id NOT IN "
        "(SELECT id FROM hook_log ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    con.commit()
