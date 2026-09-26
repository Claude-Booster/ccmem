#!/usr/bin/env python3
"""Gate: schema contract (DESIGN.md storage schema).

Every table and column that gate code, hook code, or CLI code references must
exist in the migrated schema. A missing column means a gate silently queries the
wrong field, or fails at test-time rather than at schema-design-time.

This gate is the automated catch for schema contradictions — the kind that caused
C1 and C4 in the Phase 0 review. It fails with the specific missing name so the
error is actionable without grepping the codebase.

Run: python gates/gate_schema_contract.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from _common import REPO_ROOT, GateResult

# Canonical schema from DESIGN.md. Each entry is (table, [required columns]).
# Virtual tables (FTS5) are checked for existence only, not columns.
EXPECTED_TABLES: dict[str, list[str]] = {
    "memories": [
        "id", "type", "content", "context", "subject",
        "scope", "project_id", "project_root",
        "created_at", "accessed_at", "access_count",
        "status", "supersedes", "embedding", "content_hash", "pinned",
    ],
    "candidates": [
        "id", "session_id", "prompt_id",
        "user_turn", "assistant_turn",
        "classifier_score", "is_pre_compact",
        "created_at", "status", "content_hash",
    ],
    "session_injections": [
        "session_id", "memory_id", "injected_at",
    ],
    "schema_meta": [
        "key", "value",
    ],
    "transcript_progress": [
        "transcript_path", "last_prompt_id", "last_ordinal", "session_id", "updated_at",
    ],
    "sigil_refusals": [
        "id", "transcript_path", "session_id", "created_at", "excerpt", "acknowledged_at",
    ],
}

# Virtual tables: existence check only (PRAGMA table_info returns nothing for FTS5).
EXPECTED_VIRTUAL_TABLES = ["memories_fts"]

# Columns that appear in gate SQL or gate Python code. This list is the canonical
# reference: if you add a column reference in any gate, add it here too.
GATE_COLUMN_REFERENCES: dict[str, list[str]] = {
    "memories": [
        "id",
        "content",   # gate_budget (injected text), gate_secret_hygiene
        "context",   # gate_secret_hygiene (must audit reasoning too)
        "subject",   # gate_subject_supersession (exact-match supersession)
        "scope",     # ensure_seeded_db, retrieval WHERE clause
        "project_id",   # ensure_seeded_db, scoping query
        "project_root", # ensure_seeded_db, human-readable inspection
        "created_at",   # ensure_seeded_db, ordering
        "status",    # supersession check, gate_subject_supersession
        "supersedes",   # gate_subject_supersession (chain pointer)
    ],
    "candidates": [
        "id", "session_id", "prompt_id",
        "user_turn", "assistant_turn",
        "classifier_score", "is_pre_compact",
        "created_at", "status", "content_hash",
    ],
    "session_injections": [
        "session_id", "memory_id", "injected_at",
    ],
    "schema_meta": [
        "key", "value",
    ],
}


def get_db_columns(con: sqlite3.Connection, table: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    r = GateResult("schema contract")

    try:
        from ccmem.db import connect, migrate  # type: ignore
    except Exception as exc:  # noqa: BLE001
        r.fail("ccmem.db importable", f"{type(exc).__name__}: {exc}")
        return r.report()
    r.ok("ccmem.db importable")

    # Use an in-memory DB so this gate is self-contained and leaves no state.
    try:
        con = connect(":memory:")
        migrate(con)
    except Exception as exc:  # noqa: BLE001
        r.fail("migration runs on fresh DB", f"{type(exc).__name__}: {exc}")
        return r.report()
    r.ok("migration runs on fresh DB")

    # --- table existence ---------------------------------------------------
    for table, columns in EXPECTED_TABLES.items():
        if not table_exists(con, table):
            r.fail(f"table {table!r} exists", "missing from migrated schema")
        else:
            r.ok(f"table {table!r} exists")

    for vtable in EXPECTED_VIRTUAL_TABLES:
        if not table_exists(con, vtable):
            r.fail(f"virtual table {vtable!r} exists", "missing from migrated schema")
        else:
            r.ok(f"virtual table {vtable!r} exists")

    # --- column presence ---------------------------------------------------
    for table, expected_cols in EXPECTED_TABLES.items():
        if not table_exists(con, table):
            continue
        actual = get_db_columns(con, table)
        for col in expected_cols:
            if col not in actual:
                r.fail(
                    f"{table}.{col} exists",
                    f"column missing — gate code references it; add it to the migration",
                )
            else:
                r.ok(f"{table}.{col} exists")

    # --- gate reference audit --------------------------------------------
    # Every column named in GATE_COLUMN_REFERENCES must also appear in EXPECTED_TABLES.
    # This catches typos in the reference list before they become silent wrong queries.
    for table, cols in GATE_COLUMN_REFERENCES.items():
        defined = set(EXPECTED_TABLES.get(table, []))
        for col in cols:
            if col not in defined:
                r.fail(
                    f"gate reference {table}.{col} is in contract",
                    "column is referenced in gate code but not in EXPECTED_TABLES — "
                    "add it to EXPECTED_TABLES or fix the gate reference",
                )

    # --- schema_meta seed values -----------------------------------------
    try:
        rows = {k: v for k, v in con.execute("SELECT key, value FROM schema_meta").fetchall()}
    except sqlite3.Error as exc:
        r.fail("schema_meta seeded", f"query failed: {exc}")
        rows = {}

    for key in ("schema_version", "embedding_dim"):
        if key not in rows:
            r.fail(f"schema_meta.{key} seeded", "migrate() must seed this key")
        else:
            r.ok(f"schema_meta.{key} seeded", rows[key])

    con.close()
    return r.report()


if __name__ == "__main__":
    sys.exit(main())
