from __future__ import annotations
import sqlite3


def maybe_supersede(
    con: sqlite3.Connection,
    new_id: str,
    subject: str | None,
    project_id: str,
    scope: str = "project",
) -> str | None:
    """Mark the most recent active memory with the same subject superseded.

    Supersession is scope-aware so a global preference and a project decision that
    happen to share a subject never clobber each other:
      - project scope: match scope='project' AND same project_id AND same subject.
      - global/user scope: a cross-project namespace — match on scope + subject,
        ignoring project_id (a newer global fact replaces the older one wherever it
        was first recorded).

    Returns the superseded id, or None if nothing was superseded.
    Exact-subject match only — Phase 1. KNN path comes in Phase 2.
    """
    if not subject:
        return None
    if scope == "project":
        row = con.execute(
            """
            SELECT id FROM memories
            WHERE status = 'active'
              AND scope = 'project'
              AND project_id = ?
              AND subject = ?
              AND id != ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project_id, subject, new_id),
        ).fetchone()
    else:
        row = con.execute(
            """
            SELECT id FROM memories
            WHERE status = 'active'
              AND scope = ?
              AND subject = ?
              AND id != ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (scope, subject, new_id),
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
