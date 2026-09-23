from __future__ import annotations
import argparse
import hashlib
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _db(args):
    from ccmem.db import connect, migrate
    home = os.environ.get("CCMEM_HOME", str(Path.home() / ".claude" / "ccmem"))
    Path(home).mkdir(parents=True, exist_ok=True)
    path = Path(home) / "mem.db"
    con = connect(str(path))
    migrate(con)
    return con


def _project_id(root: str) -> str:
    return hashlib.sha256(root.encode()).hexdigest()[:16]


def cmd_add(args):
    from ccmem.redact import redact
    from ccmem.supersession import maybe_supersede
    con = _db(args)
    root = args.project_root or os.getcwd()
    pid = _project_id(root)
    content = redact(args.content)
    if content != args.content:
        print("WARNING: content contained a secret pattern and was redacted.", file=sys.stderr)
    mem_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO memories (id, type, content, context, subject, scope, "
        "project_id, project_root, created_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (mem_id, args.type, content, args.context, args.subject,
         args.scope, pid, root, now, "active"),
    )
    con.commit()
    maybe_supersede(con, mem_id, args.subject, pid)
    con.close()
    print(f"Added: {mem_id}")


def cmd_list(args):
    con = _db(args)
    root = args.project_root or os.getcwd()
    pid = _project_id(root)
    rows = con.execute(
        "SELECT id, type, content, created_at FROM memories "
        "WHERE status='active' AND (scope='global' OR scope='user' OR project_id=?) "
        "ORDER BY created_at DESC",
        (pid,),
    ).fetchall()
    con.close()
    if not rows:
        print("(no memories)")
        return
    for rid, rtype, content, created in rows:
        print(f"{rid}  [{rtype}]  {content[:60]}  ({created[:10]})")


def cmd_show(args):
    con = _db(args)
    row = con.execute(
        "SELECT id, type, content, context, subject, scope, status, created_at "
        "FROM memories WHERE id=?", (args.id,)
    ).fetchone()
    con.close()
    if not row:
        print(f"Not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    rid, rtype, content, context, subject, scope, status, created = row
    print(f"ID:      {rid}")
    print(f"Type:    {rtype}  Scope: {scope}  Status: {status}")
    print(f"Created: {created[:10]}")
    if subject:
        print(f"Subject: {subject}")
    print(f"\n{content}")
    if context:
        print(f"\n--- context ---\n{context}")


def cmd_delete(args):
    con = _db(args)
    con.execute("UPDATE memories SET status='deleted' WHERE id=?", (args.id,))
    con.commit()
    con.close()
    print(f"Deleted (reversible): {args.id}")


def cmd_restore(args):
    con = _db(args)
    con.execute("UPDATE memories SET status='active' WHERE id=?", (args.id,))
    con.commit()
    con.close()
    print(f"Restored: {args.id}")


def cmd_review(args):
    con = _db(args)
    rows = con.execute(
        "SELECT id, session_id, user_turn, assistant_turn, classifier_score "
        "FROM candidates WHERE status='pending' ORDER BY created_at"
    ).fetchall()
    if not rows:
        print("No pending candidates.")
        con.close()
        return
    for cid, sess, user, asst, score in rows:
        print(f"\n--- candidate {cid} (score {score:.1f}) ---")
        print(f"User:   {user[:120]}")
        print(f"Claude: {asst[:120]}")
        action = input("Accept (a), Reject (r), Skip (s)? ").strip().lower()
        if action == "a":
            from ccmem.redact import redact
            content = input("Memory text (Enter to use assistant turn): ").strip() or asst[:200]
            content = redact(content)
            mem_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            con.execute(
                "INSERT INTO memories (id, type, content, scope, project_id, "
                "project_root, created_at, status) VALUES (?,?,?,?,?,?,?,?)",
                (mem_id, "decision", content, "project", "manual", ".", now, "active"),
            )
            con.execute("UPDATE candidates SET status='accepted' WHERE id=?", (cid,))
            con.commit()
            print(f"Stored: {mem_id}")
        elif action == "r":
            con.execute("UPDATE candidates SET status='rejected' WHERE id=?", (cid,))
            con.commit()
    con.close()


def cmd_inject(args):
    from ccmem.render import render
    from ccmem.retrieval import retrieve
    from ccmem.scoping import project_key, resolve_project_root
    con = _db(args)
    root = args.project_root or resolve_project_root(os.getcwd())
    pid, _ = project_key(root)
    memories = retrieve(con, pid)
    con.close()
    if not memories:
        print("(nothing to inject)")
        return
    block = render(memories, root, "dry-run")
    tokens = (len(block) + 3) // 4
    print(f"--- dry run ({len(memories)} memories, ~{tokens} tokens) ---")
    print(block)


def cmd_doctor(args):
    home = os.environ.get("CCMEM_HOME", str(Path.home() / ".claude" / "ccmem"))
    db_path = Path(home) / "mem.db"
    print(f"DB path:   {db_path}")
    print(f"DB exists: {db_path.exists()}")
    if not db_path.exists():
        print("Run: python -m ccmem.cli add ... to create it.")
        return
    con = _db(args)
    counts = con.execute(
        "SELECT status, COUNT(*) FROM memories GROUP BY status"
    ).fetchall()
    for status, n in counts:
        print(f"  memories [{status}]: {n}")
    pending = con.execute("SELECT COUNT(*) FROM candidates WHERE status='pending'").fetchone()[0]
    print(f"  candidates [pending]: {pending}")
    con.close()


def main():
    p = argparse.ArgumentParser(prog="ccmem")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("add", help="add a memory directly")
    a.add_argument("--type", default="decision",
                   choices=["decision", "preference", "correction", "project_state", "gotcha"])
    a.add_argument("--content", required=True)
    a.add_argument("--context")
    a.add_argument("--subject")
    a.add_argument("--scope", default="project", choices=["project", "user", "global"])
    a.add_argument("--project-root")

    lst = sub.add_parser("list", help="list active memories")
    lst.add_argument("--project-root")

    s = sub.add_parser("show", help="show memory + context")
    s.add_argument("id")

    d = sub.add_parser("delete", help="soft-delete (reversible)")
    d.add_argument("id")

    r = sub.add_parser("restore", help="restore a deleted memory")
    r.add_argument("id")

    sub.add_parser("review", help="review pending candidates")

    inj = sub.add_parser("inject", help="preview injection block")
    inj.add_argument("--dry-run", action="store_true")
    inj.add_argument("--project-root")

    sub.add_parser("doctor", help="health check")

    args = p.parse_args()
    dispatch = {
        "add": cmd_add, "list": cmd_list, "show": cmd_show,
        "delete": cmd_delete, "restore": cmd_restore,
        "review": cmd_review, "inject": cmd_inject, "doctor": cmd_doctor,
    }
    if args.cmd not in dispatch:
        p.print_help()
        sys.exit(1)
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
