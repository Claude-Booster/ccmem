from __future__ import annotations
import sqlite3


def maybe_supersede(
    con: sqlite3.Connection,
    new_id: str,
    subject: str | None,
    project_id: str,
) -> str | None:
    """Mark the most recent active memory with the same subject superseded.

    Returns the superseded id, or None if nothing was superseded.
    Exact-subject match only — Phase 1. KNN path comes in Phase 2.
    """
    if not subject:
        return None
    row = con.execute(
        """
        SELECT id FROM memories
        WHERE status = 'active'
          AND project_id = ?
          AND subject = ?
          AND id != ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (project_id, subject, new_id),
    ).fetchone()
    if row is None:
        return None
    old_id = row[0]
    con.execute(
        "UPDATE memories SET status='superseded', supersedes=? WHERE id=?",
        (new_id, old_id),
    )
    con.commit()
    return old_id
