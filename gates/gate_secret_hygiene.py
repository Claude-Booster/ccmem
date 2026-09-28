#!/usr/bin/env python3
"""Gate: secret hygiene (CLAUDE.md R8).

Memory is a durable, automatically-populated store that gets re-injected into a
context window. That is an excellent place for a leaked key to live forever.
Redaction happens on write, so a secret never lands in the table at all.

Run: python gates/gate_secret_hygiene.py
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

from _common import REPO_ROOT, TEST_HOME, GateResult, load_config

# Things that must never survive redaction. Each entry is (label, input, needle)
# where `needle` is the substring that must be gone from the output.
SECRET_CORPUS = [
    ("anthropic key", "use sk-ant-api03-AAAAbbbbCCCCddddEEEEffffGGGG for the call", "sk-ant-api03-"),
    ("openai key", "OPENAI_API_KEY=sk-proj-1234567890abcdefghijklmn", "sk-proj-"),
    ("aws access key", "creds are AKIAIOSFODNN7EXAMPLE / wJalrXUtnFEMI", "AKIAIOSFODNN7EXAMPLE"),
    ("github pat", "token ghp_16CharsOfNonsense0000000000000000", "ghp_"),
    ("slack token", "xoxb-REDACTED-TEST-FIXTURE", "xoxb-"),
    ("bearer header", "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "eyJhbGciOi"),
    ("pg url", "postgres://admin:hunter2@db.internal:5432/prod", "hunter2"),
    ("private key", "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----", "MIIEow"),
    ("env assignment", 'SUPABASE_SERVICE_ROLE_KEY="eyJzdXBhYmFzZSI6dHJ1ZX0"', "eyJzdXBhYmFzZSI6"),
    ("generic password", "password: correct-horse-battery-staple", "correct-horse-battery-staple"),
]

PRIVATE_CORPUS = [
    ("private tag", "keep this <private>my therapist's name is Dana</private> out", "Dana"),
    ("multiline private", "a\n<private>\nline one\nline two\n</private>\nb", "line two"),
]

# Patterns used to audit an existing DB for leakage.
LEAK_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{12,}"),
    re.compile(r"sk-proj-[A-Za-z0-9]{12,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"://[^\s:/]+:[^\s@/]{6,}@"),
]

GITIGNORE_REQUIRED = ["mem.db", ".ccmem", "*.db"]


def load_redactor():
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from ccmem.redact import redact  # type: ignore
    except Exception as exc:  # noqa: BLE001 -- any import failure is a gate failure
        return None, f"{type(exc).__name__}: {exc}"
    return redact, ""


def check_redaction(r: GateResult) -> None:
    redact, err = load_redactor()
    if redact is None:
        r.fail("ccmem.redact.redact importable", err)
        return
    r.ok("ccmem.redact.redact importable")

    for label, raw, needle in SECRET_CORPUS:
        try:
            out = redact(raw)
        except Exception as exc:  # noqa: BLE001
            r.fail(f"redacts {label}", f"raised {type(exc).__name__}: {exc}")
            continue
        if needle in out:
            r.fail(f"redacts {label}", f"secret survived: {out[:90]!r}")
        else:
            r.ok(f"redacts {label}")

    for label, raw, needle in PRIVATE_CORPUS:
        out = redact(raw)
        if needle in out:
            r.fail(f"strips {label}", f"content survived: {out[:90]!r}")
        else:
            r.ok(f"strips {label}")

    # Redaction must not be so aggressive it destroys ordinary text.
    benign = "we chose RLS BEFORE INSERT triggers over app-layer limits for rate limiting"
    if redact(benign) != benign:
        r.fail("leaves benign text intact", f"mangled to {redact(benign)!r}")
    else:
        r.ok("leaves benign text intact")

    # Redaction must be idempotent -- otherwise deduplication on write produces
    # inconsistent results between the first and second pass over the same text.
    once = redact(SECRET_CORPUS[0][1])
    if redact(once) != once:
        r.fail("redaction is idempotent", "second pass changed the output")
    else:
        r.ok("redaction is idempotent")


def check_gitignore(r: GateResult) -> None:
    path = REPO_ROOT / ".gitignore"
    if not path.exists():
        r.fail(".gitignore exists", "no .gitignore -- the DB will get committed")
        return
    text = path.read_text(encoding="utf-8")
    missing = [p for p in GITIGNORE_REQUIRED if p not in text]
    if missing:
        r.fail(".gitignore covers memory store", f"missing: {', '.join(missing)}")
    else:
        r.ok(".gitignore covers memory store")


def check_live_db(r: GateResult) -> None:
    """If a DB exists, audit its contents. Absence is not a pass."""
    cfg = load_config()
    candidates = [
        TEST_HOME / "mem.db",
        Path.home() / ".claude" / "ccmem" / "mem.db",
    ]
    db = next((p for p in candidates if p.exists()), None)
    if db is None:
        r.ok("no stored secrets in DB", "no DB present yet (not a pass, just nothing to audit)")
        return

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        # Audit both content (injected) and context (stored reasoning).
        # Reasoning is more likely to contain a pasted key than a one-line fact.
        rows = con.execute("SELECT id, content, context FROM memories").fetchall()
        con.close()
    except sqlite3.Error as exc:
        r.fail("no stored secrets in DB", f"could not read {db}: {exc}")
        return

    leaks = []
    for mem_id, content, context in rows:
        combined = (content or "") + "\n" + (context or "")
        for pattern in LEAK_PATTERNS:
            if pattern.search(combined):
                leaks.append(f"row {mem_id} matches {pattern.pattern[:28]}")
                break
    if leaks:
        r.fail("no stored secrets in DB", "; ".join(leaks[:5]))
    else:
        r.ok("no stored secrets in DB", f"{len(rows)} rows audited (content + context)")

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        crows = con.execute(
            "SELECT id, user_turn, assistant_turn FROM candidates"
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        r.fail("no stored secrets in candidates", f"could not read candidates in {db}: {exc}")
        crows = []
    cleaks = []
    for cid, user_turn, assistant_turn in crows:
        combined = (user_turn or "") + "\n" + (assistant_turn or "")
        for pattern in LEAK_PATTERNS:
            if pattern.search(combined):
                cleaks.append(f"candidate {cid} matches {pattern.pattern[:28]}")
                break
    if cleaks:
        r.fail("no stored secrets in candidates", "; ".join(cleaks[:5]))
    else:
        r.ok("no stored secrets in candidates", f"{len(crows)} candidates audited")


def main() -> int:
    r = GateResult("secret hygiene")
    check_redaction(r)
    check_gitignore(r)
    check_live_db(r)
    return r.report()


if __name__ == "__main__":
    sys.exit(main())
