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
    embedding    BLOB
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
    status           TEXT NOT NULL DEFAULT 'pending'
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
    excerpt     TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', '1');
INSERT OR IGNORE INTO schema_meta VALUES ('embedding_dim', '384');
"""


def connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def migrate(con: sqlite3.Connection) -> None:
    con.executescript(_DDL)


def log_hook_event(con: sqlite3.Connection, event: str, excerpt: str, keep: int = 10) -> None:
    """Append one row to hook_log and trim the table to the last `keep` rows.

    `recorded_at` uses datetime('now') inside SQLite so the value is produced
    by the DB engine, not Python — this keeps the call site cache-safe.
    """
    con.execute(
        "INSERT INTO hook_log (event, recorded_at, excerpt) "
        "VALUES (?, datetime('now'), ?)",
        (event, excerpt[:200]),
    )
    con.execute(
        "DELETE FROM hook_log WHERE id NOT IN "
        "(SELECT id FROM hook_log ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    con.commit()
