from __future__ import annotations
import sqlite3
from dataclasses import dataclass


@dataclass
class Memory:
    id: str
    type: str
    content: str
    subject: str | None
    scope: str
    created_at: str
    access_count: int


def retrieve(
    con: sqlite3.Connection,
    project_id: str,
    query: str | None = None,
    top_k: int = 12,
) -> list[Memory]:
    scope_filter = (
        "m.status = 'active' AND "
        "(m.scope = 'global' OR m.scope = 'user' OR "
        " (m.scope = 'project' AND m.project_id = ?))"
    )
    if query and query.strip():
        rows = con.execute(
            f"""
            SELECT m.id, m.type, m.content, m.subject, m.scope,
                   m.created_at, m.access_count
            FROM memories_fts fts
            JOIN memories m ON m.rowid = fts.rowid
            WHERE fts.memories_fts MATCH ? AND {scope_filter}
            ORDER BY fts.rank, m.access_count DESC, m.created_at DESC
            LIMIT ?
            """,
            (query, project_id, top_k),
        ).fetchall()
    else:
        rows = con.execute(
            f"""
            SELECT m.id, m.type, m.content, m.subject, m.scope,
                   m.created_at, m.access_count
            FROM memories m
            WHERE {scope_filter}
            ORDER BY m.access_count DESC, m.created_at DESC
            LIMIT ?
            """,
            (project_id, top_k),
        ).fetchall()
    return [Memory(*r) for r in rows]


def mark_accessed(con: sqlite3.Connection, ids: list[str]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    con.execute(
        f"UPDATE memories SET access_count = access_count + 1, "
        f"accessed_at = datetime('now') WHERE id IN ({placeholders})",
        ids,
    )
    con.commit()
