#!/usr/bin/env python3
"""Gate: inserting a memory with a duplicate subject supersedes the older row."""
from __future__ import annotations
import sys
from _common import REPO_ROOT, GateResult


def main() -> int:
    r = GateResult("subject supersession")

    sys.path.insert(0, str(REPO_ROOT))
    import hashlib
    try:
        from ccmem.db import connect, migrate
        from ccmem.supersession import maybe_supersede
    except Exception as exc:
        r.fail("ccmem imports", str(exc))
        return r.report()
    r.ok("ccmem imports")

    home = REPO_ROOT / ".ccmem-test"
    home.mkdir(exist_ok=True)
    db = home / "mem.db"
    pid = hashlib.sha256(str(REPO_ROOT).encode()).hexdigest()[:16]

    con = connect(db)
    migrate(con)

    subject = "supersession-gate-subject"
    old_id = "sup-gate-old-0001"
    new_id = "sup-gate-new-0002"

    con.execute("DELETE FROM memories WHERE id IN (?,?)", (old_id, new_id))
    con.commit()

    con.execute(
        "INSERT INTO memories "
        "(id, type, content, subject, scope, project_id, project_root, created_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (old_id, "decision", "old memory content", subject, "project",
         pid, str(REPO_ROOT), "2026-01-01T00:00:00Z", "active"),
    )
    con.commit()
    r.ok("old memory inserted", old_id)

    con.execute(
        "INSERT INTO memories "
        "(id, type, content, subject, scope, project_id, project_root, created_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (new_id, "decision", "new memory content supersedes old", subject, "project",
         pid, str(REPO_ROOT), "2026-02-01T00:00:00Z", "active"),
    )
    con.commit()

    maybe_supersede(con, new_id, subject, pid)
    r.ok("maybe_supersede called", new_id)

    old_row = con.execute(
        "SELECT status, supersedes FROM memories WHERE id=?", (old_id,)
    ).fetchone()
    new_row = con.execute(
        "SELECT status FROM memories WHERE id=?", (new_id,)
    ).fetchone()
    con.close()

    if old_row is None:
        r.fail("old row exists", f"{old_id} not found after supersession")
        return r.report()

    old_status, old_supersedes = old_row
    if old_status != "superseded":
        r.fail("old row marked superseded", f"status={old_status!r}, expected 'superseded'")
    else:
        r.ok("old row marked superseded")

    if old_supersedes != new_id:
        r.fail("supersedes pointer correct", f"supersedes={old_supersedes!r}, expected {new_id!r}")
    else:
        r.ok("supersedes pointer correct")

    if new_row is None or new_row[0] != "active":
        r.fail("new row still active", f"new row status={new_row!r}")
    else:
        r.ok("new row still active")

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
