#!/usr/bin/env python3
"""Gate: FTS5 retrieval on a seeded DB returns expected results."""
from __future__ import annotations
import hashlib
import sys
from _common import REPO_ROOT, GateResult, ensure_seeded_db


def main() -> int:
    r = GateResult("fts5 retrieval")
    db, err = ensure_seeded_db(50)
    if db is None:
        r.fail("test DB seeded", err)
        return r.report()
    r.ok("test DB seeded", str(db))

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from ccmem.db import connect
        from ccmem.retrieval import retrieve
    except Exception as exc:
        r.fail("ccmem imports", str(exc))
        return r.report()
    r.ok("ccmem imports")

    pid = hashlib.sha256(str(REPO_ROOT).encode()).hexdigest()[:16]
    con = connect(db)
    try:
        con.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        con.commit()
    except Exception:
        pass

    results = retrieve(con, pid, query="approach")
    con.close()
    if not results:
        r.fail("FTS5 query returns results", "seeded DB with 50 rows returned nothing for 'approach'")
    else:
        r.ok("FTS5 query returns results", f"{len(results)} hits")

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
