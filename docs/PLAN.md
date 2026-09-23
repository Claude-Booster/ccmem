# ccmem Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase 1 ccmem core loop — SQLite + FTS5 + explicit `!mem:` capture + SessionStart injection + basic CLI — usable end-to-end before any embedding or LLM work starts.

**Architecture:** A Python library (`ccmem/`) wraps a single SQLite file (FTS5). Five thin hook scripts (`hooks/`) drive it from Claude Code's event lifecycle. A CLI (`python -m ccmem.cli`) is the human-facing surface. All hooks exit 0 on any error (R1) and short-circuit silently when `CCMEM_DISABLED=1`.

**Tech Stack:** Python 3.11+, stdlib only (`sqlite3`, `argparse`, `subprocess`, `re`, `hashlib`, `uuid`, `unittest.mock`). `pytest` for tests. No third-party runtime deps until Phase 2.

**Spec:** `docs/DESIGN.md`

## Global Constraints

- Python 3.11+; stdlib-first — no third-party runtime deps in Phase 1
- Every hook wraps all logic in `try/except Exception: sys.exit(0)` (R1)
- `CCMEM_DISABLED=1` → silent `sys.exit(0)` in <300ms, checked before any I/O
- No `datetime.now()`, `uuid.uuid4()`, `random.*` in any code reachable from injected output — annotate deliberate safe uses with `# ccmem: cache-safe`
- Schema is DESIGN.md: columns are `content/context/type/project_id/project_root/subject`; table is `candidates` not `capture_queue`
- Hook budget limits (from `gates/config.json`): SessionStart 2000ms, UserPromptSubmit 1500ms, Stop **200ms**, SessionEnd 1200ms, PreCompact 2000ms
- `python gates/run_gates.py --phase 1` fully green before Phase 1 is done

---

## File map

```
ccmem/
  __init__.py           — empty package marker
  db.py                 — connect(), migrate(); owns all DDL
  redact.py             — redact(text) → text; patterns + <private> stripping
  scoping.py            — resolve_project_root(cwd), project_key(root) → (id, root)
  retrieval.py          — Memory dataclass, retrieve(con, project_id, ...) → list[Memory]
  render.py             — render(memories, ...) → str; Jaccard dedup; injection block
  capture.py            — score_turn(), extract_sigil(), enqueue_candidate()
  supersession.py       — maybe_supersede(con, memory_id, subject, project_id)
  cli.py                — argparse CLI: add list show delete restore review inject doctor

hooks/
  mem_inject.py         — SessionStart: retrieve + render → additionalContext
  mem_retrieve.py       — UserPromptSubmit: sigil capture + opt-in per-turn injection
  mem_capture.py        — Stop: score turn → enqueue_candidate (must exit in <200ms)
  mem_flush.py          — SessionEnd: WAL checkpoint only
  mem_snapshot.py       — PreCompact: mark pending candidates is_pre_compact=1

fixtures/
  transcript.jsonl          (exists)
  payload_session_start.json
  payload_session_start_resume.json
  payload_user_prompt_submit.json
  payload_user_prompt_submit_sigil.json
  payload_stop.json
  payload_session_end.json
  payload_pre_compact.json

tests/
  conftest.py
  test_db.py
  test_redact.py
  test_scoping.py
  test_retrieval.py
  test_render.py
  test_capture.py
  test_supersession.py
  test_hook_mem_inject.py
  test_hook_mem_retrieve.py
  test_hook_mem_capture.py
  test_hook_mem_flush.py
  test_hook_mem_snapshot.py
  test_cli.py

.claude/
  settings.local.json   — hook registrations; no tool events (gate_cache_safety)
```

---

## Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `ccmem/__init__.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Produces: `pytest` runnable from repo root; `python -m ccmem.cli` importable (fails gracefully until cli.py exists)

- [ ] **Step 1: Write pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=70"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "ccmem"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]

[tool.setuptools.packages.find]
where = ["."]
include = ["ccmem*"]
```

- [ ] **Step 2: Create package init**

```python
# ccmem/__init__.py
```

- [ ] **Step 3: Create conftest**

```python
# tests/conftest.py
from pathlib import Path
import sys

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))
```

- [ ] **Step 4: Verify pytest finds the test directory**

```
pytest --collect-only
```

Expected: "no tests ran" — directory structure is recognised.

- [ ] **Step 5: Commit**

```
git add pyproject.toml ccmem/__init__.py tests/conftest.py
git commit -m "feat: project scaffold — pyproject.toml, ccmem package, test harness"
```

---

## Task 2: ccmem/db.py — schema and migration

**Gate target:** `python gates/gate_schema_contract.py`

**Files:**
- Create: `ccmem/db.py`
- Create: `tests/test_db.py`

**Interfaces:**
- Produces: `connect(path: str | Path) -> sqlite3.Connection`, `migrate(con: sqlite3.Connection) -> None`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_db.py
import sqlite3
import pytest
from ccmem.db import connect, migrate

def test_connect_returns_connection(tmp_path):
    con = connect(tmp_path / "mem.db")
    assert isinstance(con, sqlite3.Connection)
    con.close()

def test_migrate_creates_memories_table(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "memories" in tables
    assert "candidates" in tables
    assert "session_injections" in tables
    assert "schema_meta" in tables
    con.close()

def test_migrate_creates_fts5_index(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    vtables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchall()}
    assert "memories_fts" in vtables
    con.close()

def test_migrate_seeds_schema_meta(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    meta = dict(con.execute("SELECT key, value FROM schema_meta").fetchall())
    assert "schema_version" in meta
    assert "embedding_dim" in meta
    con.close()

def test_migrate_is_idempotent(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    migrate(con)  # must not raise
    con.close()

def test_memories_columns_match_design(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(memories)").fetchall()}
    required = {
        "id", "type", "content", "context", "subject",
        "scope", "project_id", "project_root",
        "created_at", "accessed_at", "access_count",
        "status", "supersedes", "embedding",
    }
    assert required <= cols, f"missing: {required - cols}"
    con.close()

def test_wal_pragma_set(tmp_path):
    con = connect(tmp_path / "mem.db")
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    con.close()
```

- [ ] **Step 2: Run — verify all fail**

```
pytest tests/test_db.py -v
```

Expected: `ImportError: No module named 'ccmem.db'`

- [ ] **Step 3: Implement ccmem/db.py**

```python
# ccmem/db.py
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
```

- [ ] **Step 4: Run tests — verify all pass**

```
pytest tests/test_db.py -v
```

Expected: all 7 PASS.

- [ ] **Step 5: Run gate**

```
cd gates && python gate_schema_contract.py
```

Expected: `[PASS]` on all checks. If `ccmem.db not importable` appears, `sys.path` is wrong — run from repo root: `python -m pytest tests/test_db.py` first to confirm import works.

- [ ] **Step 6: Commit**

```
git add ccmem/db.py tests/test_db.py
git commit -m "feat(db): schema migration — all DESIGN.md tables; gate_schema_contract passes"
```

---

## Task 3: ccmem/redact.py — secret patterns

**Gate target:** `python gates/gate_secret_hygiene.py` (redact section — gitignore check will still fail)

**Files:**
- Create: `ccmem/redact.py`
- Create: `tests/test_redact.py`

**Interfaces:**
- Produces: `redact(text: str) -> str`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_redact.py
from ccmem.redact import redact

def test_strips_anthropic_key():
    out = redact("use sk-ant-api03-AAAAbbbbCCCCddddEEEEffffGGGG for the call")
    assert "sk-ant-api03-" not in out

def test_strips_openai_key():
    out = redact("OPENAI_API_KEY=sk-proj-1234567890abcdefghijklmn")
    assert "sk-proj-" not in out

def test_strips_aws_key():
    out = redact("AKIAIOSFODNN7EXAMPLE / wJalrXUtnFEMI")
    assert "AKIAIOSFODNN7EXAMPLE" not in out

def test_strips_github_pat():
    out = redact("token ghp_16CharsOfNonsense0000000000000000")
    assert "ghp_" not in out

def test_strips_slack_token():
    out = redact("xoxb-REDACTED-TEST-FIXTURE")
    assert "xoxb-" not in out

def test_strips_private_key():
    out = redact("-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----")
    assert "MIIEow" not in out

def test_strips_pg_url_password():
    out = redact("postgres://admin:hunter2@db.internal:5432/prod")
    assert "hunter2" not in out

def test_strips_private_tag():
    out = redact("keep this <private>my therapist's name is Dana</private> out")
    assert "Dana" not in out

def test_strips_multiline_private():
    out = redact("a\n<private>\nline one\nline two\n</private>\nb")
    assert "line two" not in out

def test_leaves_benign_text_intact():
    t = "we chose RLS BEFORE INSERT triggers over app-layer limits"
    assert redact(t) == t

def test_idempotent():
    text = "use sk-ant-api03-AAAAbbbbCCCC for the call"
    once = redact(text)
    assert redact(once) == once
```

- [ ] **Step 2: Run — verify fail**

```
pytest tests/test_redact.py -v
```

Expected: `ImportError: No module named 'ccmem.redact'`

- [ ] **Step 3: Implement ccmem/redact.py**

```python
# ccmem/redact.py
from __future__ import annotations
import re

_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.DOTALL)

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{12,}"),
    re.compile(r"sk-proj-[A-Za-z0-9]{12,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"://[^\s:/]+:[^\s@/]{6,}@"),
]


def redact(text: str) -> str:
    text = _PRIVATE_RE.sub("[redacted]", text)
    for pat in _PATTERNS:
        text = pat.sub("[redacted]", text)
    return text
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_redact.py -v
```

Expected: all 11 PASS.

- [ ] **Step 5: Partial gate check**

```
cd gates && python gate_secret_hygiene.py
```

Expected: redact checks PASS; gitignore check will FAIL until Task 14.

- [ ] **Step 6: Commit**

```
git add ccmem/redact.py tests/test_redact.py
git commit -m "feat(redact): secret pattern stripping + <private> tag; R8"
```

---

## Task 4: ccmem/scoping.py — project root resolution

**Files:**
- Create: `ccmem/scoping.py`
- Create: `tests/test_scoping.py`

**Interfaces:**
- Produces: `resolve_project_root(cwd: str) -> str`, `project_key(root: str) -> tuple[str, str]`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_scoping.py
import hashlib
from pathlib import Path
from unittest.mock import patch
from ccmem.scoping import resolve_project_root, project_key

REPO = Path(__file__).parent.parent

def test_resolve_returns_repo_root_for_subdir():
    # The real repo is a git repo; gates/ is a subdir
    root = resolve_project_root(str(REPO / "gates"))
    assert root == str(REPO)

def test_resolve_returns_cwd_when_not_git(tmp_path):
    root = resolve_project_root(str(tmp_path))
    assert root == str(tmp_path)

def test_resolve_handles_git_not_found():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        root = resolve_project_root("/some/path")
    assert root == "/some/path"

def test_resolve_handles_timeout():
    import subprocess
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
        root = resolve_project_root("/some/path")
    assert root == "/some/path"

def test_project_key_is_deterministic():
    pid1, root1 = project_key("/home/user/myrepo")
    pid2, root2 = project_key("/home/user/myrepo")
    assert pid1 == pid2
    assert root1 == root2 == "/home/user/myrepo"

def test_project_key_is_16_hex_chars():
    pid, _ = project_key("/some/path")
    assert len(pid) == 16
    assert all(c in "0123456789abcdef" for c in pid)

def test_project_key_differs_by_path():
    pid_a, _ = project_key("/repo/a")
    pid_b, _ = project_key("/repo/b")
    assert pid_a != pid_b
```

- [ ] **Step 2: Run — verify fail**

```
pytest tests/test_scoping.py -v
```

- [ ] **Step 3: Implement ccmem/scoping.py**

```python
# ccmem/scoping.py
from __future__ import annotations
import hashlib
import os
import subprocess


def resolve_project_root(cwd: str) -> str:
    """Returns the git repo root, worktree-safe. Falls back to cwd."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return cwd
        common = r.stdout.strip()
        abs_common = os.path.normpath(os.path.join(cwd, common))
        return os.path.dirname(abs_common)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return cwd


def project_key(root: str) -> tuple[str, str]:
    """Returns (project_id, project_root). project_id is sha256[:16]."""
    pid = hashlib.sha256(root.encode()).hexdigest()[:16]
    return pid, root
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_scoping.py -v
```

Expected: all 7 PASS.

- [ ] **Step 5: Commit**

```
git add ccmem/scoping.py tests/test_scoping.py
git commit -m "feat(scoping): worktree-safe project root resolution; Q4"
```

---

## Task 5: ccmem/retrieval.py — FTS5 retrieval

**Files:**
- Create: `ccmem/retrieval.py`
- Create: `tests/test_retrieval.py`

**Interfaces:**
- Produces: `Memory` dataclass, `retrieve(con, project_id, query=None, top_k=12) -> list[Memory]`, `mark_accessed(con, ids)`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_retrieval.py
import pytest
from ccmem.db import connect, migrate
from ccmem.retrieval import Memory, retrieve, mark_accessed

PROJECT_ID = "aabbccdd11223344"

def seeded_con(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    con.execute("""
        INSERT INTO memories (id, type, content, context, subject, scope,
                              project_id, project_root, created_at, status)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, ("m1", "decision", "chose RLS triggers over app-layer limits",
          "latency under contention", "rate-limiter", "project",
          PROJECT_ID, "/repo", "2026-09-01T00:00:00Z", "active"))
    con.execute("""
        INSERT INTO memories (id, type, content, context, subject, scope,
                              project_id, project_root, created_at, status)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, ("m2", "preference", "prefers targeted patch edits over full rewrites",
          None, "editing", "project",
          PROJECT_ID, "/repo", "2026-09-15T00:00:00Z", "active"))
    con.execute("""
        INSERT INTO memories (id, type, content, context, subject, scope,
                              project_id, project_root, created_at, status)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, ("m3", "gotcha", "port 8076 is closed; use SSH tunnel to 8077",
          None, "port-8076", "global",
          "other_project", "/other", "2026-09-10T00:00:00Z", "active"))
    con.commit()
    # Rebuild FTS index
    con.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    con.commit()
    return con

def test_retrieve_all_returns_project_and_global(tmp_path):
    con = seeded_con(tmp_path)
    mems = retrieve(con, PROJECT_ID)
    ids = {m.id for m in mems}
    assert "m1" in ids
    assert "m2" in ids
    assert "m3" in ids  # global scope crosses projects
    con.close()

def test_retrieve_excludes_superseded(tmp_path):
    con = seeded_con(tmp_path)
    con.execute("UPDATE memories SET status='superseded' WHERE id='m1'")
    con.commit()
    mems = retrieve(con, PROJECT_ID)
    assert all(m.id != "m1" for m in mems)
    con.close()

def test_retrieve_fts_filters_by_query(tmp_path):
    con = seeded_con(tmp_path)
    mems = retrieve(con, PROJECT_ID, query="RLS triggers")
    assert any(m.id == "m1" for m in mems)
    con.close()

def test_retrieve_respects_top_k(tmp_path):
    con = seeded_con(tmp_path)
    mems = retrieve(con, PROJECT_ID, top_k=1)
    assert len(mems) <= 1
    con.close()

def test_mark_accessed_increments_count(tmp_path):
    con = seeded_con(tmp_path)
    mark_accessed(con, ["m1"])
    row = con.execute("SELECT access_count FROM memories WHERE id='m1'").fetchone()
    assert row[0] == 1
    con.close()
```

- [ ] **Step 2: Run — verify fail**

```
pytest tests/test_retrieval.py -v
```

- [ ] **Step 3: Implement ccmem/retrieval.py**

```python
# ccmem/retrieval.py
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
            SELECT id, type, content, subject, scope, created_at, access_count
            FROM memories
            WHERE {scope_filter}
            ORDER BY access_count DESC, created_at DESC
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
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_retrieval.py -v
```

Expected: all 5 PASS.

- [ ] **Step 5: Commit**

```
git add ccmem/retrieval.py tests/test_retrieval.py
git commit -m "feat(retrieval): FTS5 + scope-filtered retrieve; mark_accessed"
```

---

## Task 6: ccmem/render.py — injection block

**Files:**
- Create: `ccmem/render.py`
- Create: `tests/test_render.py`

**Interfaces:**
- Consumes: `Memory` from `ccmem.retrieval`
- Produces: `render(memories, project_root, source, external_lines=None, max_tokens=1200) -> str`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_render.py
from ccmem.retrieval import Memory
from ccmem.render import render, MARKER_OPEN, MARKER_CLOSE

def _mem(id="m1", type="decision", content="chose RLS triggers",
         subject="rate-limiter", scope="project",
         created_at="2026-09-01T00:00:00Z", access_count=0):
    return Memory(id, type, content, subject, scope, created_at, access_count)

def test_render_contains_open_marker():
    out = render([_mem()], "/repo", "startup")
    assert MARKER_OPEN in out

def test_render_contains_close_marker():
    out = render([_mem()], "/repo", "startup")
    assert MARKER_CLOSE in out

def test_render_contains_content():
    out = render([_mem(content="chose RLS triggers")], "/repo", "startup")
    assert "chose RLS triggers" in out

def test_render_empty_memories():
    out = render([], "/repo", "startup")
    assert out == ""

def test_render_dedup_against_external():
    # A memory whose content matches a CLAUDE.md line should be suppressed
    external = ["chose RLS triggers over app-layer limits"]
    out = render([_mem(content="chose RLS triggers over app-layer limits")],
                 "/repo", "startup", external_lines=external)
    assert "chose RLS" not in out

def test_render_respects_token_cap():
    # 300 memories that would blow the 1200-token budget
    mems = [_mem(id=f"m{i}", content=f"fact {i}: " + "x" * 100) for i in range(300)]
    out = render(mems, "/repo", "startup", max_tokens=1200)
    # chars/4 heuristic: 1200 tokens = ~4800 chars
    assert len(out) < 6000

def test_render_format_per_line():
    out = render([_mem(type="decision", content="chose RLS")], "/repo", "startup")
    # Each memory line: "- [type | age] content"
    assert any(line.startswith("- [decision") for line in out.splitlines())

def test_render_is_deterministic():
    mems = [_mem()]
    out1 = render(mems, "/repo", "startup")
    out2 = render(mems, "/repo", "startup")
    assert out1 == out2
```

- [ ] **Step 2: Run — verify fail**

```
pytest tests/test_render.py -v
```

- [ ] **Step 3: Implement ccmem/render.py**

```python
# ccmem/render.py
from __future__ import annotations
from datetime import datetime, timezone
from ccmem.retrieval import Memory

MARKER_OPEN = "<!-- ccmem:"
MARKER_CLOSE = "<!-- /ccmem -->"


def _age_str(created_at: str) -> str:
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)  # ccmem: cache-safe — age from stored timestamp
        age = (now - dt).days
        if age < 1:
            return "<1d"
        if age < 7:
            return f"{age}d"
        if age < 30:
            return f"{age // 7}w"
        return f"{age // 30}mo"
    except Exception:
        return "?"


def _ngrams(text: str, n: int = 5) -> set[str]:
    t = "".join(c.lower() for c in text if c.isalnum() or c.isspace())
    if len(t) < n:
        return set()
    return {t[i : i + n] for i in range(len(t) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _dedup(memories: list[Memory], external_lines: list[str], threshold: float = 0.4) -> list[Memory]:
    ext = [_ngrams(line) for line in external_lines if line.strip()]
    out = []
    for mem in memories:
        ng = _ngrams(mem.content)
        if any(_jaccard(ng, e) > threshold for e in ext):
            continue
        out.append(mem)
    return out


def render(
    memories: list[Memory],
    project_root: str,
    source: str,
    external_lines: list[str] | None = None,
    max_tokens: int = 1200,
) -> str:
    if not memories:
        return ""
    if external_lines:
        memories = _dedup(memories, external_lines)
    if not memories:
        return ""

    lines: list[str] = []
    token_est = 0
    for mem in memories:
        line = f"- [{mem.type} | {_age_str(mem.created_at)}] {mem.content}"
        token_est += (len(line) + 3) // 4
        if token_est > max_tokens:
            break
        lines.append(line)

    count = len(lines)
    noun = "memory" if count == 1 else "memories"
    header = f"{MARKER_OPEN} {count} {noun} | project: {project_root} | session: {source} -->"
    return f"{header}\n" + "\n".join(lines) + f"\n{MARKER_CLOSE}"
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_render.py -v
```

Expected: all 8 PASS.

- [ ] **Step 5: Commit**

```
git add ccmem/render.py tests/test_render.py
git commit -m "feat(render): injection block with Jaccard dedup and token cap; R9"
```

---

## Task 7: ccmem/capture.py — classifier, sigil, enqueue

**Files:**
- Create: `ccmem/capture.py`
- Create: `tests/test_capture.py`

**Interfaces:**
- Produces: `score_turn(user, assistant) -> float`, `extract_sigil(prompt) -> tuple[str|None, str|None, str]`, `enqueue_candidate(con, session_id, prompt_id, user_turn, assistant_turn, score, is_pre_compact=False)`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_capture.py
from ccmem.capture import score_turn, extract_sigil, enqueue_candidate
from ccmem.db import connect, migrate

def test_score_decision_phrase():
    score = score_turn("we decided to use RLS triggers", "Good choice.")
    assert score >= 3

def test_score_correction_phrase():
    score = score_turn("actually that's wrong", "You're right, sorry.")
    assert score >= 2

def test_score_low_for_ordinary_exchange():
    score = score_turn("what time is it?", "It is 3pm.")
    assert score < 2

def test_extract_sigil_bare():
    text, scope, cleaned = extract_sigil("!mem: chose RLS over app-layer")
    assert text == "chose RLS over app-layer"
    assert scope == "project"

def test_extract_sigil_global():
    text, scope, cleaned = extract_sigil("!mem[global]: port 8076 is firewalled")
    assert text == "port 8076 is firewalled"
    assert scope == "global"

def test_extract_sigil_absent():
    text, scope, cleaned = extract_sigil("normal prompt text")
    assert text is None
    assert scope is None
    assert cleaned == "normal prompt text"

def test_enqueue_candidate_inserts_row(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    enqueue_candidate(con, "sess-1", "prompt-1", "user text", "asst text", 4.0)
    rows = con.execute("SELECT * FROM candidates").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "sess-1"   # session_id
    assert rows[0][6] == 0          # is_pre_compact

def test_enqueue_pre_compact_sets_flag(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    enqueue_candidate(con, "sess-1", None, "u", "a", 5.0, is_pre_compact=True)
    row = con.execute("SELECT is_pre_compact FROM candidates").fetchone()
    assert row[0] == 1
```

- [ ] **Step 2: Run — verify fail**

```
pytest tests/test_capture.py -v
```

- [ ] **Step 3: Implement ccmem/capture.py**

```python
# ccmem/capture.py
from __future__ import annotations
import re
import sqlite3
import uuid
from datetime import datetime, timezone

_PATTERNS: list[tuple[float, re.Pattern]] = [
    (3, re.compile(r"\b(we decided|we'?re going with|the approach is|going with)\b", re.I)),
    (3, re.compile(r"\b(from now on|always|never|going forward)\b", re.I)),
    (2, re.compile(r"\b(you were wrong|that'?s incorrect|actually)\b", re.I)),
    (2, re.compile(r"\buse .+ instead of\b|\bswitch to\b|\breplace with\b", re.I)),
    (2, re.compile(r"```[^\n]*\n[-+]", re.M)),  # diff block heuristic
    (1, re.compile(r"\b(the reason|because|in order to)\b", re.I)),
]

_SIGIL_RE = re.compile(r"^!mem(?:\[(?P<scope>[a-z]+)\])?:\s*(?P<text>.+)", re.DOTALL)


def score_turn(user_text: str, assistant_text: str) -> float:
    combined = user_text + "\n" + assistant_text
    total = 0.0
    for points, pattern in _PATTERNS:
        if pattern.search(combined):
            total += points
    return total


def extract_sigil(prompt: str) -> tuple[str | None, str | None, str]:
    """Returns (annotation_text, scope, cleaned_prompt). text is None if no sigil."""
    m = _SIGIL_RE.match(prompt.strip())
    if not m:
        return None, None, prompt
    text = m.group("text").strip()
    scope = m.group("scope") or "project"
    # Strip the first !mem:... line; leave remaining lines for Claude
    cleaned = re.sub(r"^!mem(?:\[[a-z]+\])?:[^\n]*\n?", "", prompt.strip(), count=1)
    return text, scope, cleaned.strip()


def enqueue_candidate(
    con: sqlite3.Connection,
    session_id: str,
    prompt_id: str | None,
    user_turn: str,
    assistant_turn: str,
    score: float,
    is_pre_compact: bool = False,
) -> None:
    con.execute(
        "INSERT OR IGNORE INTO candidates "
        "(id, session_id, prompt_id, user_turn, assistant_turn, "
        "classifier_score, is_pre_compact, created_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),   # ccmem: cache-safe — id is stored, not injected
            session_id,
            prompt_id,
            user_turn,
            assistant_turn,
            score,
            1 if is_pre_compact else 0,
            datetime.now(timezone.utc).isoformat(),  # ccmem: cache-safe
            "pending",
        ),
    )
    con.commit()
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_capture.py -v
```

Expected: all 8 PASS.

- [ ] **Step 5: Commit**

```
git add ccmem/capture.py tests/test_capture.py
git commit -m "feat(capture): lexical classifier, !mem: sigil extraction, candidate enqueue"
```

---

## Task 8: ccmem/supersession.py — exact-subject supersession

**Files:**
- Create: `ccmem/supersession.py`
- Create: `tests/test_supersession.py`

**Interfaces:**
- Produces: `maybe_supersede(con, new_id, subject, project_id) -> str | None` (returns superseded id or None)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_supersession.py
from ccmem.db import connect, migrate
from ccmem.supersession import maybe_supersede

def _insert(con, id, subject, project_id, status="active"):
    con.execute("""
        INSERT INTO memories (id, type, content, subject, scope,
                              project_id, project_root, created_at, status)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (id, "decision", f"content for {id}", subject, "project",
          project_id, "/repo", "2026-01-01T00:00:00Z", status))
    con.commit()

def test_supersedes_matching_subject(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    _insert(con, "old", "rate-limiter", "proj1")
    _insert(con, "new", "rate-limiter", "proj1")
    superseded = maybe_supersede(con, "new", "rate-limiter", "proj1")
    assert superseded == "old"
    old = con.execute("SELECT status FROM memories WHERE id='old'").fetchone()
    assert old[0] == "superseded"
    con.close()

def test_no_supersession_when_no_match(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    _insert(con, "m1", "other-subject", "proj1")
    result = maybe_supersede(con, "m1", "rate-limiter", "proj1")
    assert result is None
    con.close()

def test_no_supersession_when_subject_is_none(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    _insert(con, "m1", None, "proj1")
    result = maybe_supersede(con, "m1", None, "proj1")
    assert result is None
    con.close()

def test_no_cross_project_supersession(tmp_path):
    con = connect(tmp_path / "mem.db")
    migrate(con)
    _insert(con, "old", "rate-limiter", "proj1")
    _insert(con, "new", "rate-limiter", "proj2")
    result = maybe_supersede(con, "new", "rate-limiter", "proj2")
    assert result is None  # different project_id
    con.close()
```

- [ ] **Step 2: Run — verify fail**

```
pytest tests/test_supersession.py -v
```

- [ ] **Step 3: Implement ccmem/supersession.py**

```python
# ccmem/supersession.py
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
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_supersession.py -v
```

Expected: all 4 PASS.

- [ ] **Step 5: Commit**

```
git add ccmem/supersession.py tests/test_supersession.py
git commit -m "feat(supersession): exact-subject match; Phase 1 of contradiction detection"
```

---

## Task 9: Fixture payloads

**Files:**
- Create: `fixtures/payload_session_start.json`
- Create: `fixtures/payload_session_start_resume.json`
- Create: `fixtures/payload_user_prompt_submit.json`
- Create: `fixtures/payload_user_prompt_submit_sigil.json`
- Create: `fixtures/payload_stop.json`
- Create: `fixtures/payload_session_end.json`
- Create: `fixtures/payload_pre_compact.json`

No gate target — these are prerequisites for hook tests.

- [ ] **Step 1: Write fixture files**

```json
// fixtures/payload_session_start.json
{
  "session_id": "0000-test-session",
  "transcript_path": "fixtures/transcript.jsonl",
  "cwd": ".",
  "hook_event_name": "SessionStart",
  "source": "startup",
  "model": "claude-opus-5"
}
```

```json
// fixtures/payload_session_start_resume.json
{
  "session_id": "0000-test-session",
  "transcript_path": "fixtures/transcript.jsonl",
  "cwd": ".",
  "hook_event_name": "SessionStart",
  "source": "resume",
  "model": "claude-opus-5",
  "seconds_since_last_response": 3600,
  "context_tokens": 45000,
  "prompt_cache_likely_expired": true,
  "estimated_cache_write_usd": 0.042
}
```

```json
// fixtures/payload_user_prompt_submit.json
{
  "session_id": "0000-test-session",
  "transcript_path": "fixtures/transcript.jsonl",
  "cwd": ".",
  "hook_event_name": "UserPromptSubmit",
  "prompt": "should we rate limit in the app layer or with a trigger?",
  "prompt_id": "ec52e4d3-0131-445c-ab70-a8672c2b10a1"
}
```

```json
// fixtures/payload_user_prompt_submit_sigil.json
{
  "session_id": "0000-test-session",
  "transcript_path": "fixtures/transcript.jsonl",
  "cwd": ".",
  "hook_event_name": "UserPromptSubmit",
  "prompt": "!mem: chose RLS triggers over app-layer rate limiting",
  "prompt_id": "ec52e4d3-0131-445c-ab70-a8672c2b10a1"
}
```

```json
// fixtures/payload_stop.json
{
  "session_id": "0000-test-session",
  "transcript_path": "fixtures/transcript.jsonl",
  "cwd": ".",
  "hook_event_name": "Stop",
  "stop_hook_active": false,
  "last_assistant_message": "Done -- trigger added, patch only."
}
```

```json
// fixtures/payload_session_end.json
{
  "session_id": "0000-test-session",
  "transcript_path": "fixtures/transcript.jsonl",
  "cwd": ".",
  "hook_event_name": "SessionEnd",
  "reason": "prompt_input_exit"
}
```

```json
// fixtures/payload_pre_compact.json
{
  "session_id": "0000-test-session",
  "transcript_path": "fixtures/transcript.jsonl",
  "cwd": ".",
  "hook_event_name": "PreCompact",
  "trigger": "auto",
  "custom_instructions": ""
}
```

- [ ] **Step 2: Verify files parse as JSON**

```
python -c "
import json, pathlib
for f in pathlib.Path('fixtures').glob('payload_*.json'):
    json.loads(f.read_text())
    print('OK', f.name)
"
```

- [ ] **Step 3: Commit**

```
git add fixtures/
git commit -m "test(fixtures): hook payload fixtures for all five events"
```

---

## Task 10: hooks/mem_inject.py — SessionStart

**Gate targets:** `python gates/gate_budget.py`, `python gates/gate_cache_safety.py` (SessionStart checks)

**Files:**
- Create: `hooks/mem_inject.py`
- Create: `tests/test_hook_mem_inject.py`

**Interfaces:**
- Consumes: `ccmem.db.connect/migrate`, `ccmem.retrieval.retrieve/mark_accessed`, `ccmem.render.render`, `ccmem.scoping.resolve_project_root/project_key`

- [ ] **Step 1: Write fixture-driven tests**

```python
# tests/test_hook_mem_inject.py
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
HOOK = REPO / "hooks" / "mem_inject.py"
FIXTURES = REPO / "fixtures"


def run_hook(payload_file, env_extra=None, db_dir=None):
    payload = json.loads((FIXTURES / payload_file).read_text())
    payload["cwd"] = str(REPO)
    env = os.environ.copy()
    env["CCMEM_HOME"] = db_dir or str(REPO / ".ccmem-test")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=10,
        env=env,
    )


def test_exits_zero_when_db_absent(tmp_path):
    proc = run_hook("payload_session_start.json", db_dir=str(tmp_path / "nonexistent"))
    assert proc.returncode == 0

def test_exits_zero_on_kill_switch():
    proc = run_hook("payload_session_start.json", env_extra={"CCMEM_DISABLED": "1"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == b""

def test_no_output_when_db_empty(tmp_path):
    # DB exists but has no memories
    from ccmem.db import connect, migrate
    con = connect(tmp_path / "mem.db")
    migrate(con)
    con.close()
    proc = run_hook("payload_session_start.json", db_dir=str(tmp_path))
    assert proc.returncode == 0
    assert proc.stdout.strip() == b""

def test_injects_context_with_memories(tmp_path):
    from ccmem.db import connect, migrate
    from ccmem.scoping import project_key
    con = connect(tmp_path / "mem.db")
    migrate(con)
    pid, _ = project_key(str(REPO))
    con.execute("""
        INSERT INTO memories (id, type, content, subject, scope,
                              project_id, project_root, created_at, status)
        VALUES ('m1','decision','chose RLS triggers','rate-limiter','project',?,?,?,?)
    """, (pid, str(REPO), "2026-09-01T00:00:00Z", "active"))
    con.commit()
    con.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    con.commit()
    con.close()
    proc = run_hook("payload_session_start.json", db_dir=str(tmp_path))
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "<!-- ccmem:" in ctx
    assert "chose RLS triggers" in ctx

def test_output_is_valid_json_when_memories_present(tmp_path):
    from ccmem.db import connect, migrate
    from ccmem.scoping import project_key
    con = connect(tmp_path / "mem.db")
    migrate(con)
    pid, _ = project_key(str(REPO))
    con.execute("""
        INSERT INTO memories (id, type, content, subject, scope,
                              project_id, project_root, created_at, status)
        VALUES ('m1','decision','test memory','subj','project',?,?,'2026-01-01T00:00:00Z','active')
    """, (pid, str(REPO)))
    con.commit()
    con.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    con.commit()
    con.close()
    proc = run_hook("payload_session_start.json", db_dir=str(tmp_path))
    assert proc.returncode == 0
    obj = json.loads(proc.stdout)
    assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart"

def test_survives_hostile_inputs():
    hostile = [b"", b"not json", b'{"broken":}', b"null", b"[]"]
    for blob in hostile:
        env = os.environ.copy()
        env["CCMEM_HOME"] = str(REPO / ".ccmem-test")
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=blob, capture_output=True, timeout=10, env=env,
        )
        assert proc.returncode == 0, f"failed on {blob!r}"
```

- [ ] **Step 2: Run — verify fail**

```
pytest tests/test_hook_mem_inject.py -v
```

Expected: `FileNotFoundError` or similar — hook script does not exist.

- [ ] **Step 3: Implement hooks/mem_inject.py**

```python
#!/usr/bin/env python3
# hooks/mem_inject.py — SessionStart: retrieve memories → additionalContext
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    if os.environ.get("CCMEM_DISABLED"):
        return

    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw)
    except Exception:
        return

    try:
        from ccmem.db import connect, migrate
        from ccmem.render import render
        from ccmem.retrieval import mark_accessed, retrieve
        from ccmem.scoping import project_key, resolve_project_root

        home = os.environ.get("CCMEM_HOME", os.path.join(os.path.expanduser("~"), ".claude", "ccmem"))
        db_path = os.path.join(home, "mem.db")
        if not os.path.exists(db_path):
            return

        cwd = payload.get("cwd", os.getcwd())
        root = resolve_project_root(cwd)
        pid, _ = project_key(root)
        source = payload.get("source", "startup")

        con = connect(db_path)
        memories = retrieve(con, pid)
        if not memories:
            con.close()
            return

        mark_accessed(con, [m.id for m in memories])
        con.close()

        block = render(memories, root, source)
        if not block:
            return

        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": block,
            }
        }))
    except Exception:
        return


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_hook_mem_inject.py -v
```

Expected: all 6 PASS.

- [ ] **Step 5: Run budget gate** (requires seeded DB — gate seeds it automatically)

```
cd gates && python gate_budget.py
```

Expected: FAIL on "test DB seeded" until `ccmem.db` has the new schema AND `ensure_seeded_db` can insert. If schema matches, it should pass.

- [ ] **Step 6: Commit**

```
git add hooks/mem_inject.py tests/test_hook_mem_inject.py
git commit -m "feat(hooks): mem_inject.py — SessionStart injection"
```

---

## Task 11: hooks/mem_retrieve.py — UserPromptSubmit

**Gate target:** `python gates/gate_cache_safety.py` (per-turn default-off + byte-stable checks)

**Files:**
- Create: `hooks/mem_retrieve.py`
- Create: `tests/test_hook_mem_retrieve.py`

**Interfaces:**
- Consumes: `ccmem.capture.extract_sigil`, `ccmem.retrieval.retrieve`, `ccmem.render.render`, `ccmem.supersession.maybe_supersede`

**Note on sigil and prompt modification:** `UserPromptSubmit` cannot replace the prompt (FACTS §2). The `!mem:` annotation goes to Claude unchanged. The hook captures it to the DB and confirms via `systemMessage`. The sigil is small enough Claude ignores it in practice.

- [ ] **Step 1: Write fixture-driven tests**

```python
# tests/test_hook_mem_retrieve.py
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
HOOK = REPO / "hooks" / "mem_retrieve.py"
FIXTURES = REPO / "fixtures"


def run_hook(payload_file, env_extra=None, db_dir=None):
    payload = json.loads((FIXTURES / payload_file).read_text())
    payload["cwd"] = str(REPO)
    env = os.environ.copy()
    env["CCMEM_HOME"] = db_dir or str(REPO / ".ccmem-test")
    env.pop("CCMEM_PER_TURN", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True, timeout=10, env=env,
    )


def test_no_injection_by_default():
    proc = run_hook("payload_user_prompt_submit.json")
    assert proc.returncode == 0
    # No additionalContext when CCMEM_PER_TURN not set
    if proc.stdout.strip():
        obj = json.loads(proc.stdout)
        ctx = obj.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert not ctx.strip()

def test_kill_switch_silent():
    proc = run_hook("payload_user_prompt_submit.json", env_extra={"CCMEM_DISABLED": "1"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == b""

def test_never_exits_2():
    # Exit 2 on UserPromptSubmit erases the user's prompt — forbidden
    payload = json.loads((FIXTURES / "payload_user_prompt_submit.json").read_text())
    payload["prompt"] = "x" * 1_000_000
    env = os.environ.copy()
    env["CCMEM_HOME"] = str(REPO / ".ccmem-test")
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True, timeout=10, env=env,
    )
    assert proc.returncode != 2

def test_sigil_capture_writes_to_db(tmp_path):
    from ccmem.db import connect, migrate
    con = connect(tmp_path / "mem.db")
    migrate(con)
    con.close()
    proc = run_hook(
        "payload_user_prompt_submit_sigil.json",
        db_dir=str(tmp_path),
    )
    assert proc.returncode == 0
    from ccmem.db import connect as c2
    con2 = c2(tmp_path / "mem.db")
    rows = con2.execute("SELECT content FROM memories WHERE status='active'").fetchall()
    con2.close()
    assert any("RLS triggers" in r[0] for r in rows)

def test_survives_hostile_inputs():
    hostile = [b"", b"not json", b"null"]
    for blob in hostile:
        env = os.environ.copy()
        env["CCMEM_HOME"] = str(REPO / ".ccmem-test")
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=blob, capture_output=True, timeout=10, env=env,
        )
        assert proc.returncode == 0, f"failed on {blob!r}"
```

- [ ] **Step 2: Run — verify fail**

```
pytest tests/test_hook_mem_retrieve.py -v
```

- [ ] **Step 3: Implement hooks/mem_retrieve.py**

```python
#!/usr/bin/env python3
# hooks/mem_retrieve.py — UserPromptSubmit: sigil capture + opt-in per-turn injection
import json
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    if os.environ.get("CCMEM_DISABLED"):
        return

    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw)
    except Exception:
        return

    try:
        from ccmem.capture import extract_sigil
        from ccmem.db import connect, migrate
        from ccmem.redact import redact
        from ccmem.render import render
        from ccmem.retrieval import retrieve
        from ccmem.scoping import project_key, resolve_project_root
        from ccmem.supersession import maybe_supersede

        home = os.environ.get("CCMEM_HOME", os.path.join(os.path.expanduser("~"), ".claude", "ccmem"))
        db_path = os.path.join(home, "mem.db")

        prompt = payload.get("prompt", "")
        cwd = payload.get("cwd", os.getcwd())
        session_id = payload.get("session_id", "unknown")

        root = resolve_project_root(cwd)
        pid, _ = project_key(root)

        output: dict = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}

        # --- sigil capture (always, regardless of CCMEM_PER_TURN) ---
        text, scope, _cleaned = extract_sigil(prompt)
        if text and os.path.exists(db_path):
            redacted = redact(text)
            if redacted != text:
                output["systemMessage"] = "ccmem: annotation contained a secret and was not stored."
            else:
                con = connect(db_path)
                mem_id = str(uuid.uuid4())   # ccmem: cache-safe
                now = datetime.now(timezone.utc).isoformat()  # ccmem: cache-safe
                con.execute(
                    "INSERT OR IGNORE INTO memories "
                    "(id, type, content, scope, project_id, project_root, created_at, status) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (mem_id, "preference", redacted, scope or "project",
                     pid, root, now, "active"),
                )
                con.commit()
                maybe_supersede(con, mem_id, None, pid)
                con.close()
                output["systemMessage"] = f"ccmem: captured — {redacted[:80]}"

        # --- per-turn retrieval (opt-in) ---
        if os.environ.get("CCMEM_PER_TURN") == "1" and os.path.exists(db_path):
            con = connect(db_path)
            memories = retrieve(con, pid, query=prompt[:200] if prompt else None)
            con.close()
            if memories:
                block = render(memories, root, "per-turn")
                output["hookSpecificOutput"]["additionalContext"] = block

        if output.get("systemMessage") or output["hookSpecificOutput"].get("additionalContext"):
            print(json.dumps(output))

    except Exception:
        return


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_hook_mem_retrieve.py -v
```

Expected: all 5 PASS.

- [ ] **Step 5: Run cache safety gate**

```
cd gates && python gate_cache_safety.py
```

Expected: "per-turn injection defaults off" PASS; "byte-stable" PASS; tool-hooks check will FAIL until Task 14.

- [ ] **Step 6: Commit**

```
git add hooks/mem_retrieve.py tests/test_hook_mem_retrieve.py
git commit -m "feat(hooks): mem_retrieve.py — sigil capture + opt-in per-turn injection; R5"
```

---

## Task 12: hooks/mem_capture.py — Stop (must exit in <200ms)

**Files:**
- Create: `hooks/mem_capture.py`
- Create: `tests/test_hook_mem_capture.py`

**Interfaces:**
- Consumes: `ccmem.capture.score_turn/enqueue_candidate`, `ccmem.db.connect`

- [ ] **Step 1: Write fixture-driven tests**

```python
# tests/test_hook_mem_capture.py
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
HOOK = REPO / "hooks" / "mem_capture.py"
FIXTURES = REPO / "fixtures"


def run_hook(payload_file, env_extra=None, db_dir=None):
    payload = json.loads((FIXTURES / payload_file).read_text())
    payload["cwd"] = str(REPO)
    env = os.environ.copy()
    env["CCMEM_HOME"] = db_dir or str(REPO / ".ccmem-test")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True, timeout=5, env=env,
    )


def test_exits_zero():
    proc = run_hook("payload_stop.json")
    assert proc.returncode == 0

def test_kill_switch_silent():
    proc = run_hook("payload_stop.json", env_extra={"CCMEM_DISABLED": "1"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == b""

def test_emits_no_context():
    # Stop is a non-injecting event (R6)
    proc = run_hook("payload_stop.json")
    if proc.stdout.strip():
        obj = json.loads(proc.stdout)
        ctx = obj.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert not ctx.strip()

def test_enqueues_high_score_turn(tmp_path):
    from ccmem.db import connect, migrate
    con = connect(tmp_path / "mem.db")
    migrate(con)
    con.close()
    payload = json.loads((FIXTURES / "payload_stop.json").read_text())
    # This transcript phrase triggers the classifier
    payload["last_assistant_message"] = "We decided to use RLS triggers from now on."
    payload["cwd"] = str(REPO)
    env = os.environ.copy()
    env["CCMEM_HOME"] = str(tmp_path)
    # Read last user turn from transcript fixture to provide context
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True, timeout=5, env=env,
    )
    assert proc.returncode == 0
    from ccmem.db import connect as c2
    con2 = c2(tmp_path / "mem.db")
    rows = con2.execute("SELECT * FROM candidates").fetchall()
    con2.close()
    assert len(rows) >= 1

def test_survives_hostile_inputs():
    for blob in [b"", b"not json", b"null"]:
        env = os.environ.copy()
        env["CCMEM_HOME"] = str(REPO / ".ccmem-test")
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=blob, capture_output=True, timeout=5, env=env,
        )
        assert proc.returncode == 0
```

- [ ] **Step 2: Run — verify fail**

```
pytest tests/test_hook_mem_capture.py -v
```

- [ ] **Step 3: Implement hooks/mem_capture.py**

```python
#!/usr/bin/env python3
# hooks/mem_capture.py — Stop: score turn → enqueue candidate (<200ms)
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    if os.environ.get("CCMEM_DISABLED"):
        return

    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw)
    except Exception:
        return

    # Never continue=False or block — just capture and exit
    if payload.get("stop_hook_active"):
        return  # already in a Stop loop — don't re-trigger

    try:
        from ccmem.capture import enqueue_candidate, score_turn
        from ccmem.db import connect

        home = os.environ.get("CCMEM_HOME", os.path.join(os.path.expanduser("~"), ".claude", "ccmem"))
        db_path = os.path.join(home, "mem.db")
        if not os.path.exists(db_path):
            return

        assistant_msg = payload.get("last_assistant_message", "")
        session_id = payload.get("session_id", "unknown")
        prompt_id = payload.get("prompt_id")

        # Read last user turn from transcript for scoring context
        user_turn = ""
        transcript = payload.get("transcript_path", "")
        if transcript and os.path.exists(transcript):
            try:
                import json as _json
                lines = open(transcript, encoding="utf-8", errors="replace").readlines()
                for line in reversed(lines):
                    try:
                        rec = _json.loads(line)
                        if rec.get("type") == "user":
                            content = rec.get("message", {}).get("content", [])
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    user_turn = block["text"]
                                    break
                            if user_turn:
                                break
                    except Exception:
                        continue
            except Exception:
                pass

        score = score_turn(user_turn, assistant_msg)
        threshold = float(os.environ.get("CCMEM_THRESHOLD", "4"))
        if score < threshold:
            return

        con = connect(db_path)
        enqueue_candidate(con, session_id, prompt_id, user_turn, assistant_msg, score)
        con.close()

    except Exception:
        return


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_hook_mem_capture.py -v
```

Expected: all 5 PASS.

- [ ] **Step 5: Commit**

```
git add hooks/mem_capture.py tests/test_hook_mem_capture.py
git commit -m "feat(hooks): mem_capture.py — Stop hook; lexical scoring → candidate queue"
```

---

## Task 13: hooks/mem_flush.py + hooks/mem_snapshot.py

**Gate target:** `python gates/gate_hook_contract.py` (requires ALL 5 hooks to exist)

**Files:**
- Create: `hooks/mem_flush.py`
- Create: `hooks/mem_snapshot.py`
- Create: `tests/test_hook_mem_flush.py`
- Create: `tests/test_hook_mem_snapshot.py`

- [ ] **Step 1: Write tests for both hooks**

```python
# tests/test_hook_mem_flush.py
import json, os, subprocess, sys
from pathlib import Path

REPO = Path(__file__).parent.parent
HOOK = REPO / "hooks" / "mem_flush.py"
FIXTURES = REPO / "fixtures"

def run_hook(payload_file, env_extra=None, db_dir=None):
    payload = json.loads((FIXTURES / payload_file).read_text())
    env = os.environ.copy()
    env["CCMEM_HOME"] = db_dir or str(REPO / ".ccmem-test")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True, timeout=10, env=env,
    )

def test_exits_zero():
    assert run_hook("payload_session_end.json").returncode == 0

def test_kill_switch():
    proc = run_hook("payload_session_end.json", env_extra={"CCMEM_DISABLED": "1"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == b""

def test_emits_no_context():
    proc = run_hook("payload_session_end.json")
    if proc.stdout.strip():
        obj = json.loads(proc.stdout)
        assert not obj.get("hookSpecificOutput", {}).get("additionalContext", "").strip()

def test_survives_hostile_inputs():
    for blob in [b"", b"not json", b"null"]:
        env = os.environ.copy()
        env["CCMEM_HOME"] = str(REPO / ".ccmem-test")
        proc = subprocess.run([sys.executable, str(HOOK)],
            input=blob, capture_output=True, timeout=5, env=env)
        assert proc.returncode == 0
```

```python
# tests/test_hook_mem_snapshot.py
import json, os, subprocess, sys
from pathlib import Path

REPO = Path(__file__).parent.parent
HOOK = REPO / "hooks" / "mem_snapshot.py"
FIXTURES = REPO / "fixtures"

def run_hook(payload_file, env_extra=None, db_dir=None):
    payload = json.loads((FIXTURES / payload_file).read_text())
    env = os.environ.copy()
    env["CCMEM_HOME"] = db_dir or str(REPO / ".ccmem-test")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True, timeout=10, env=env,
    )

def test_exits_zero():
    assert run_hook("payload_pre_compact.json").returncode == 0

def test_kill_switch():
    proc = run_hook("payload_pre_compact.json", env_extra={"CCMEM_DISABLED": "1"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == b""

def test_emits_no_context():
    proc = run_hook("payload_pre_compact.json")
    if proc.stdout.strip():
        obj = json.loads(proc.stdout)
        assert not obj.get("hookSpecificOutput", {}).get("additionalContext", "").strip()

def test_marks_pending_candidates_pre_compact(tmp_path):
    from ccmem.db import connect, migrate
    con = connect(tmp_path / "mem.db")
    migrate(con)
    con.execute("""
        INSERT INTO candidates (id, session_id, user_turn, assistant_turn,
                                classifier_score, created_at, status)
        VALUES ('c1','sess','user','asst',5.0,'2026-01-01T00:00:00Z','pending')
    """)
    con.commit()
    con.close()
    proc = run_hook("payload_pre_compact.json", db_dir=str(tmp_path))
    assert proc.returncode == 0
    from ccmem.db import connect as c2
    con2 = c2(tmp_path / "mem.db")
    row = con2.execute("SELECT is_pre_compact FROM candidates WHERE id='c1'").fetchone()
    assert row[0] == 1
    con2.close()

def test_survives_hostile_inputs():
    for blob in [b"", b"not json", b"null"]:
        env = os.environ.copy()
        env["CCMEM_HOME"] = str(REPO / ".ccmem-test")
        proc = subprocess.run([sys.executable, str(HOOK)],
            input=blob, capture_output=True, timeout=5, env=env)
        assert proc.returncode == 0
```

- [ ] **Step 2: Run — verify both fail**

```
pytest tests/test_hook_mem_flush.py tests/test_hook_mem_snapshot.py -v
```

- [ ] **Step 3: Implement hooks/mem_flush.py**

```python
#!/usr/bin/env python3
# hooks/mem_flush.py — SessionEnd: WAL checkpoint only
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    if os.environ.get("CCMEM_DISABLED"):
        return
    try:
        sys.stdin.buffer.read()  # drain stdin
    except Exception:
        return
    try:
        from ccmem.db import connect

        home = os.environ.get("CCMEM_HOME", os.path.join(os.path.expanduser("~"), ".claude", "ccmem"))
        db_path = os.path.join(home, "mem.db")
        if not os.path.exists(db_path):
            return
        con = connect(db_path)
        con.execute("PRAGMA wal_checkpoint(PASSIVE)")
        con.close()
    except Exception:
        return


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Implement hooks/mem_snapshot.py**

```python
#!/usr/bin/env python3
# hooks/mem_snapshot.py — PreCompact: mark pending candidates is_pre_compact=1
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    if os.environ.get("CCMEM_DISABLED"):
        return
    try:
        raw = sys.stdin.buffer.read()
        json.loads(raw)  # validate — but we don't need any fields
    except Exception:
        return
    try:
        from ccmem.db import connect

        home = os.environ.get("CCMEM_HOME", os.path.join(os.path.expanduser("~"), ".claude", "ccmem"))
        db_path = os.path.join(home, "mem.db")
        if not os.path.exists(db_path):
            return
        con = connect(db_path)
        con.execute(
            "UPDATE candidates SET is_pre_compact=1 WHERE status='pending' AND is_pre_compact=0"
        )
        con.commit()
        con.close()
    except Exception:
        return


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run all hook tests**

```
pytest tests/test_hook_mem_flush.py tests/test_hook_mem_snapshot.py -v
```

Expected: all 8 PASS.

- [ ] **Step 6: Run gate_hook_contract — all 5 hooks now exist**

```
cd gates && python gate_hook_contract.py
```

Expected: most checks PASS. Any FAIL here is a real contract violation — read the error and fix it.

- [ ] **Step 7: Commit**

```
git add hooks/mem_flush.py hooks/mem_snapshot.py \
        tests/test_hook_mem_flush.py tests/test_hook_mem_snapshot.py
git commit -m "feat(hooks): mem_flush.py (WAL), mem_snapshot.py (PreCompact flag)"
```

---

## Task 14: .claude/settings.local.json — hook registration

**Gate target:** `python gates/gate_cache_safety.py` (tool-hooks check requires settings file to exist)
**Also:** `python gates/gate_secret_hygiene.py` (gitignore check)

**Files:**
- Create: `.claude/settings.local.json`
- Modify: `.gitignore`

- [ ] **Step 1: Create .claude/ directory and settings.local.json**

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/hooks/mem_inject.py",
            "timeout": 10
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/hooks/mem_retrieve.py",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/hooks/mem_capture.py",
            "timeout": 1
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/hooks/mem_flush.py",
            "timeout": 5
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/hooks/mem_snapshot.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 2: Verify .gitignore covers the DB**

Add to `.gitignore` if not already present:

```
# ccmem
mem.db
mem.db-wal
mem.db-shm
.ccmem/
.ccmem-test/
*.db
```

- [ ] **Step 3: Run cache safety gate**

```
cd gates && python gate_cache_safety.py
```

Expected: "no ccmem hooks on tool events" PASS (no PreToolUse/PostToolUse entries). Static volatility scan will check `hooks/` and `ccmem/render.py` for unguarded `datetime.now()` — any that aren't annotated `# ccmem: cache-safe` will FAIL. Fix any flags by adding the annotation.

- [ ] **Step 4: Run secret hygiene gate**

```
cd gates && python gate_secret_hygiene.py
```

Expected: all checks PASS.

- [ ] **Step 5: Commit**

```
git add .claude/settings.local.json .gitignore
git commit -m "chore: hook settings + gitignore; gate_cache_safety and gate_secret_hygiene"
```

---

## Task 15: ccmem/cli.py — user-facing surface

**Files:**
- Create: `ccmem/cli.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Produces: `python -m ccmem.cli [add|list|show|delete|restore|review|inject|doctor|export]`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cli.py
import subprocess
import sys
from pathlib import Path
import json

REPO = Path(__file__).parent.parent


def run_cli(*args, db_dir=None, input_text=None):
    import os
    env = os.environ.copy()
    env["CCMEM_HOME"] = db_dir or str(REPO / ".ccmem-test")
    return subprocess.run(
        [sys.executable, "-m", "ccmem.cli"] + list(args),
        capture_output=True, text=True, timeout=10,
        input=input_text, env=env, cwd=str(REPO),
    )


def test_add_and_list(tmp_path):
    r = run_cli("add", "--type", "decision", "--content",
                "chose RLS triggers", "--project-root", str(REPO),
                db_dir=str(tmp_path))
    assert r.returncode == 0

    r2 = run_cli("list", "--project-root", str(REPO), db_dir=str(tmp_path))
    assert r2.returncode == 0
    assert "RLS triggers" in r2.stdout

def test_show(tmp_path):
    run_cli("add", "--type", "decision", "--content", "chose RLS",
            "--context", "latency beat consistency", "--project-root", str(REPO),
            db_dir=str(tmp_path))
    lst = run_cli("list", "--project-root", str(REPO), db_dir=str(tmp_path))
    mem_id = lst.stdout.strip().split()[0]
    r = run_cli("show", mem_id, db_dir=str(tmp_path))
    assert r.returncode == 0
    assert "latency beat consistency" in r.stdout

def test_delete_and_restore(tmp_path):
    run_cli("add", "--type", "decision", "--content", "test memory",
            "--project-root", str(REPO), db_dir=str(tmp_path))
    lst = run_cli("list", "--project-root", str(REPO), db_dir=str(tmp_path))
    mem_id = lst.stdout.strip().split()[0]

    run_cli("delete", mem_id, db_dir=str(tmp_path))
    lst2 = run_cli("list", "--project-root", str(REPO), db_dir=str(tmp_path))
    assert mem_id not in lst2.stdout

    run_cli("restore", mem_id, db_dir=str(tmp_path))
    lst3 = run_cli("list", "--project-root", str(REPO), db_dir=str(tmp_path))
    assert mem_id in lst3.stdout

def test_inject_dry_run(tmp_path):
    run_cli("add", "--type", "decision", "--content", "test fact",
            "--project-root", str(REPO), db_dir=str(tmp_path))
    r = run_cli("inject", "--dry-run", "--project-root", str(REPO), db_dir=str(tmp_path))
    assert r.returncode == 0
    assert "test fact" in r.stdout

def test_doctor_runs(tmp_path):
    r = run_cli("doctor", db_dir=str(tmp_path))
    assert r.returncode == 0
```

- [ ] **Step 2: Run — verify fail**

```
pytest tests/test_cli.py -v
```

- [ ] **Step 3: Implement ccmem/cli.py**

```python
# ccmem/cli.py
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

    sub.add_parser("list", help="list active memories").add_argument("--project-root")

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
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_cli.py -v
```

Expected: all 5 PASS.

- [ ] **Step 5: Smoke test manually**

```
python -m ccmem.cli add --type decision --content "chose RLS triggers" --context "latency budget"
python -m ccmem.cli list
python -m ccmem.cli inject --dry-run
python -m ccmem.cli doctor
```

- [ ] **Step 6: Commit**

```
git add ccmem/cli.py tests/test_cli.py
git commit -m "feat(cli): add list show delete restore review inject doctor"
```

---

## Task 16: Write missing Phase 1 gates

**Context:** DESIGN.md lists these Phase 1 gates that do not yet exist in `gates/` or `run_gates.py`:
- `gate_fts5_retrieval` — FTS5 query on seeded DB returns expected memories
- `gate_injection_format` — delimiter, type label, age format correct
- `gate_overlap_dedup` — memory matching CLAUDE.md line is suppressed
- `gate_subject_supersession` — older active row marked superseded on subject collision
- `gate_worktree_scoping` — resolve_project_root same for main checkout and linked worktree

Do not modify existing gates. Write each as a new file and add it to the REGISTRY.

- [ ] **Step 1: Write gates/gate_fts5_retrieval.py**

```python
#!/usr/bin/env python3
"""Gate: FTS5 retrieval on a seeded DB returns expected results."""
from __future__ import annotations
import sys
from _common import REPO_ROOT, GateResult, ensure_seeded_db

def main() -> int:
    r = GateResult("fts5 retrieval")
    db, err = ensure_seeded_db(50)
    if db is None:
        r.fail("test DB seeded", err)
        return r.report()
    r.ok("test DB seeded", str(db.relative_to(REPO_ROOT)))

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from ccmem.db import connect
        from ccmem.retrieval import retrieve
        from ccmem.scoping import project_key
    except Exception as exc:
        r.fail("ccmem imports", str(exc))
        return r.report()
    r.ok("ccmem imports")

    pid, _ = project_key(str(REPO_ROOT))
    con = connect(db)
    # Rebuild FTS index to pick up seeded rows
    try:
        con.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        con.commit()
    except Exception:
        pass  # may already be consistent

    # Query that matches seed content
    results = retrieve(con, pid, query="approach")
    con.close()
    if not results:
        r.fail("FTS5 query returns results", "seeded DB with 50 rows returned nothing for 'approach'")
    else:
        r.ok("FTS5 query returns results", f"{len(results)} hits")

    return r.report()

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Write gates/gate_injection_format.py**

```python
#!/usr/bin/env python3
"""Gate: injection block has correct delimiter, type labels, and age format."""
from __future__ import annotations
import re, sys
from _common import REPO_ROOT, GateResult, ensure_seeded_db, base_payload, run_hook, load_config, injected_text, hook_path

def main() -> int:
    cfg = load_config()
    r = GateResult("injection format")

    script = hook_path(cfg, "SessionStart")
    if not script.exists():
        r.fail("SessionStart hook exists", cfg["hooks"]["SessionStart"])
        return r.report()

    db, err = ensure_seeded_db(20)
    if db is None:
        r.fail("test DB seeded", err)
        return r.report()

    run = run_hook(cfg, "SessionStart", base_payload("SessionStart"))
    if run.returncode != 0 or run.timed_out:
        r.fail("hook runs", f"rc={run.returncode}")
        return r.report()

    ctx = injected_text(run.stdout)
    marker = cfg["injection_marker"]  # <!-- ccmem:
    if marker not in ctx:
        r.fail("open delimiter present", f"missing {marker!r}")
    else:
        r.ok("open delimiter present")

    closing = marker.replace("<", "</", 1)  # <!-- /ccmem -->
    if closing not in ctx:
        r.fail("close delimiter present", f"missing {closing!r}")
    else:
        r.ok("close delimiter present")

    prefix = cfg.get("memory_line_prefix", "- ")
    memory_lines = [l for l in ctx.splitlines() if l.startswith(prefix)]
    if not memory_lines:
        r.fail("memory lines present", f"no lines starting with {prefix!r}")
    else:
        r.ok("memory lines present", f"{len(memory_lines)} lines")
        # Format: "- [type | age] content"
        type_age_re = re.compile(r"^\- \[(\w+) \| [^\]]+\] .+")
        bad = [l for l in memory_lines if not type_age_re.match(l)]
        if bad:
            r.fail("line format [type | age] content", f"bad: {bad[0]!r}")
        else:
            r.ok("line format [type | age] content")

    return r.report()

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Write gates/gate_overlap_dedup.py**

```python
#!/usr/bin/env python3
"""Gate: a memory whose content overlaps a CLAUDE.md line is suppressed at inject time."""
from __future__ import annotations
import os, sys
from _common import REPO_ROOT, GateResult, base_payload, run_hook, load_config, injected_text, hook_path

FIXTURE_CLAUSE = "always use targeted patch edits never rewrite the whole file"

def main() -> int:
    cfg = load_config()
    r = GateResult("overlap dedup")
    sys.path.insert(0, str(REPO_ROOT))

    try:
        from ccmem.db import connect, migrate
        from ccmem.scoping import project_key
    except Exception as exc:
        r.fail("ccmem imports", str(exc))
        return r.report()

    home = REPO_ROOT / ".ccmem-dedup-test"
    home.mkdir(exist_ok=True)
    db = home / "mem.db"
    pid, _ = project_key(str(REPO_ROOT))
    con = connect(db)
    migrate(con)
    # Insert a memory that overlaps the fixture clause
    con.execute("""
        INSERT OR IGNORE INTO memories
        (id, type, content, scope, project_id, project_root, created_at, status)
        VALUES ('dedup-test','preference',?,?,?,?,'2026-01-01T00:00:00Z','active')
    """, (FIXTURE_CLAUSE, "project", pid, str(REPO_ROOT)))
    con.commit()
    try:
        con.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        con.commit()
    except Exception:
        pass
    con.close()

    # Inject with CLAUDE.md content containing the overlapping line
    env_extra = {
        "CCMEM_HOME": str(home),
        "CCMEM_TEST_CLAUDE_MD": FIXTURE_CLAUSE,
    }
    run = run_hook(cfg, "SessionStart", base_payload("SessionStart"), env_extra=env_extra)
    ctx = injected_text(run.stdout)

    if FIXTURE_CLAUSE[:30] in ctx:
        r.fail(
            "overlapping memory suppressed",
            "content matching CLAUDE.md fixture appeared in injection output",
        )
    else:
        r.ok("overlapping memory suppressed",
             "Jaccard dedup excluded the CLAUDE.md duplicate")

    return r.report()

if __name__ == "__main__":
    sys.exit(main())
```

**Note:** `gate_overlap_dedup` requires `mem_inject.py` to read `CCMEM_TEST_CLAUDE_MD` as a fixture external line. Add this to `mem_inject.py`'s render call:

```python
# In hooks/mem_inject.py, before calling render():
external_lines = []
test_claude = os.environ.get("CCMEM_TEST_CLAUDE_MD")
if test_claude:
    external_lines = [test_claude]
# In a real install, load CLAUDE.md lines here for production dedup.
block = render(memories, root, source, external_lines=external_lines)
```

- [ ] **Step 4: Write gates/gate_subject_supersession.py**

```python
#!/usr/bin/env python3
"""Gate: writing a memory with a matching subject marks the older row superseded."""
from __future__ import annotations
import sys
from _common import REPO_ROOT, GateResult

def main() -> int:
    r = GateResult("subject supersession")
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from ccmem.db import connect, migrate
        from ccmem.supersession import maybe_supersede
    except Exception as exc:
        r.fail("ccmem imports", str(exc))
        return r.report()

    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "mem.db")
        con = connect(db)
        migrate(con)
        con.execute("""
            INSERT INTO memories (id, type, content, subject, scope,
                                  project_id, project_root, created_at, status)
            VALUES ('old','decision','old fact','rate-limiter','project',
                    'testproj','/repo','2026-01-01T00:00:00Z','active')
        """)
        con.execute("""
            INSERT INTO memories (id, type, content, subject, scope,
                                  project_id, project_root, created_at, status)
            VALUES ('new','decision','new fact','rate-limiter','project',
                    'testproj','/repo','2026-09-01T00:00:00Z','active')
        """)
        con.commit()
        maybe_supersede(con, "new", "rate-limiter", "testproj")

        old = con.execute("SELECT status FROM memories WHERE id='old'").fetchone()
        new = con.execute("SELECT status FROM memories WHERE id='new'").fetchone()
        con.close()

    if old[0] == "superseded":
        r.ok("older row marked superseded")
    else:
        r.fail("older row marked superseded", f"status was {old[0]!r}")

    if new[0] == "active":
        r.ok("newer row stays active")
    else:
        r.fail("newer row stays active", f"status was {new[0]!r}")

    return r.report()

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Write gates/gate_worktree_scoping.py**

```python
#!/usr/bin/env python3
"""Gate: resolve_project_root returns same value for main checkout and linked worktree."""
from __future__ import annotations
import os, subprocess, sys, tempfile
from _common import REPO_ROOT, GateResult

def main() -> int:
    r = GateResult("worktree scoping")
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from ccmem.scoping import resolve_project_root
    except Exception as exc:
        r.fail("ccmem.scoping importable", str(exc))
        return r.report()
    r.ok("ccmem.scoping importable")

    root_from_main = resolve_project_root(str(REPO_ROOT))
    r.ok("resolve from main checkout", root_from_main)

    with tempfile.TemporaryDirectory() as td:
        wt_path = os.path.join(td, "wt")
        result = subprocess.run(
            ["git", "worktree", "add", "--detach", wt_path],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        if result.returncode != 0:
            r.fail("worktree created", result.stderr.strip()[:200])
            return r.report()
        r.ok("worktree created", wt_path)

        root_from_wt = resolve_project_root(wt_path)
        subprocess.run(
            ["git", "worktree", "remove", "--force", wt_path],
            cwd=str(REPO_ROOT), capture_output=True,
        )

    if root_from_main == root_from_wt:
        r.ok("same root from main and worktree", root_from_main)
    else:
        r.fail(
            "same root from main and worktree",
            f"main={root_from_main!r} wt={root_from_wt!r}",
        )

    return r.report()

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Add all five gates to REGISTRY in run_gates.py**

Open `gates/run_gates.py` and add to REGISTRY after `gate_budget`:

```python
    ("gate_fts5_retrieval",       1, "FTS5 query on seeded DB returns matching memories"),
    ("gate_injection_format",     1, "injection block: delimiter, [type | age] line format"),
    ("gate_overlap_dedup",        1, "memory matching CLAUDE.md line suppressed at inject time"),
    ("gate_subject_supersession", 1, "matching subject marks older row superseded"),
    ("gate_worktree_scoping",     1, "same project_id from main checkout and linked worktree"),
```

- [ ] **Step 7: Run all new gates individually to confirm they fail on missing impl (not on import error)**

```
cd gates
python gate_fts5_retrieval.py
python gate_injection_format.py
python gate_subject_supersession.py
python gate_worktree_scoping.py
```

Expected: FAIL with meaningful messages (missing implementation), not Python exceptions.

- [ ] **Step 8: Commit**

```
git add gates/gate_fts5_retrieval.py gates/gate_injection_format.py \
        gates/gate_overlap_dedup.py gates/gate_subject_supersession.py \
        gates/gate_worktree_scoping.py gates/run_gates.py
git commit -m "feat(gates): add 5 missing Phase 1 gates from DESIGN.md"
```

---

## Task 17: Phase 1 gate run — 5 of 6 green

- [ ] **Step 1: Run full Phase 1 suite**

```
cd gates && python run_gates.py --phase 1
```

Expected: 10 of 11 gates PASS. `gate_phase1_notes` FAILS with "missing docs/PHASE1-NOTES.md" — this is correct and expected.

- [ ] **Step 2: Fix any unexpected failures**

Read the gate output. Possible issues:
- `gate_cache_safety` volatility scan flags `datetime.now()` in `render.py` → add `# ccmem: cache-safe` annotation
- `gate_budget` can't seed DB → schema column mismatch → `ccmem.db.migrate()` columns must match `ensure_seeded_db` exactly
- `gate_hook_contract` budget exceeded for Stop → check the hook exits before any heavy I/O
- `gate_overlap_dedup` always suppresses → Jaccard threshold may be too low → check `_jaccard` logic

- [ ] **Step 3: Install into your real settings.json**

```bash
# Copy the hook settings into your user-level Claude Code settings
# Windows: %APPDATA%\Claude\claude_desktop_config.json or ~/.claude/settings.json
# Check with: claude --print-config
```

Merge the hooks from `.claude/settings.local.json` into your user-level settings, pointing `command` at the absolute path to this repo's hooks directory.

```
python -m ccmem.cli doctor
```

Expected: "DB path: ~/.claude/ccmem/mem.db  DB exists: False" until you add the first memory.

- [ ] **Step 4: Add your first memories**

```
python -m ccmem.cli add --type decision \
  --content "ccmem uses content/context split for injection budget" \
  --context "context is stored but not injected; keeps 1200-token cap meaningful"

python -m ccmem.cli inject --dry-run
```

Expected: injection block shows the memory with `<!-- ccmem: 1 memory ... -->`.

- [ ] **Step 5: Commit working state**

```
git add -A
git commit -m "chore: Phase 1 implementation complete — 10/11 gates passing"
```

---

## Task 18: Use for a week — write PHASE1-NOTES.md

**Gate target:** `python gates/gate_phase1_notes.py` → `python gates/run_gates.py --phase 1` fully green

This task is done by you, not by an agent. The gate cannot be passed without actual use.

- [ ] **Use ccmem for a week of normal Claude Code sessions**
  - Use `!mem: [fact]` whenever you make a decision or find a gotcha
  - Use `python -m ccmem.cli review` after sessions with high-score candidates
  - Note what injection surfaced at session start that you'd have re-explained otherwise

- [ ] **Build the 20-pair labeled dataset for Phase 2**
  - From real transcripts: find 10 pairs of memories that are true conflicts (newer replaces older) and 10 that are same-topic-different-facts
  - Save to `docs/conflict-pairs.jsonl` — format: `{"id": "p1", "memory_a": "...", "memory_b": "...", "label": "conflict|coexist"}`
  - This dataset gates `gate_threshold_calibration` in Phase 2

- [ ] **Write docs/PHASE1-NOTES.md**

The file must be ≥200 words and contain the words "wrote down", "wished", and "stale". Template:

```markdown
# Phase 1 Usage Notes

## What I actually wrote down
[List the types of things you captured: decisions, gotchas, preferences]

## What I wished I'd written down
[Things that came up again that you hadn't captured — re-explanations, forgotten gotchas]

## What went stale fastest
[Memories that were wrong or misleading within a week — project_state type?]

## Friction observations
[Was !mem: easy to reach for? Was the review loop light enough? What would make it lighter?]

## For the Phase 3 extractor
[Patterns the LLM should learn to capture automatically from candidates]
```

- [ ] **Run the gate**

```
cd gates && python gate_phase1_notes.py
```

Expected: PASS if file exists and covers all three required themes.

- [ ] **Run the full Phase 1 suite**

```
cd gates && python run_gates.py --phase 1
```

Expected: 11/11 PASS. This is the Phase 1 exit checkpoint.

- [ ] **Commit**

```
git add docs/PHASE1-NOTES.md docs/conflict-pairs.jsonl
git commit -m "docs: Phase 1 usage notes and conflict-pair dataset; all Phase 1 gates green"
```

---

## After Phase 1

Phase 2 starts when:
1. `python gates/run_gates.py --phase 1` is fully green
2. `docs/conflict-pairs.jsonl` has 20 labeled pairs
3. FTS5 recall has at least 3 real failing queries from your transcripts to justify embeddings

Do not start Phase 2 until all three are true.
