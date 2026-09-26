# ccmem generate — Implementation Plan (rev 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `ccmem generate` — a two-tier markdown file writer that replaces hook-based injection for environments where hooks are blocked (`allowManagedHooksOnly: true`).

**Architecture:** `ccmem generate` writes two static markdown files from the DB: `~/.claude/ccmem-memories.md` (global+user scope, ≤400 tokens) and `<project>/.ccmem/memories.md` (project scope, ≤800 tokens). Claude Code @imports them at session start with no hooks required. Files are written atomically, content is deterministic for any DB state, and the `.ccmem/` directory is self-protected by its own `.gitignore`.

**Tech stack:** stdlib only (os, pathlib, sqlite3). No new deps.

**Spec:** `docs/DESIGN.md` (injection format, scope query). Gate rules in `gates/README.md`.

## Revision note (rev 2 — six changes from review)

1. **Rank ossification fixed.** Selection ≠ render order. Select the *most-recent* memories that fit the cap (recency DESC), then render that set sorted `created_at ASC, id ASC`. New memories can always displace old ones; both phases deterministic.
2. **Gitignore-before-memories.** `generate_project` establishes `.ccmem/` protection *first*, verifies it, and only then writes `memories.md`. If protection can't be established (and not explicitly waived), it writes nothing and raises.
3. **Self-contained `.ccmem/.gitignore` = `*`.** ccmem never touches the repo's tracked `.gitignore`. It writes `.ccmem/.gitignore` containing `*`, which ignores the whole directory (including itself and the `.tmp` files) — no CRLF handling, trivially idempotent, zero footprint in tracked files.
4. **Cap = whole file.** Header, footer, and the count line all count against the cap. `gate_budget` asserts on the file as written.
5. **Truncation is visible.** Every block carries a deterministic count line: `<!-- ccmem: 12 of 47 memories shown (400 token cap) -->`. Doctor warns when `shown < total`.
6. **Dead hook gates retired.** `gate_hook_contract`, `gate_recovery_budget`, `gate_injection_format`, and `gate_cache_safety` test hooks that `allowManagedHooksOnly` permanently blocks. They are retired in their own commit with the reasoning recorded in FACTS.md; the R1 exit-0 and hostile-input discipline moves to a CLI-entrypoint test.

## Revision note (rev 3 — four follow-ups from the Task 1–2 review)

1. **Pinned is now real.** A `pinned INTEGER NOT NULL DEFAULT 0` column is added to `memories` (schema_version → 3, idempotent ALTER). Selection order is `pinned DESC, created_at DESC, id DESC`; render order unchanged. Pinned memories survive truncation over newer unpinned ones. If pinned memories alone exceed the cap they still truncate (hard ceiling) and the count line shows `shown < total`, which doctor surfaces as a loud warning.
2. **Gitignore verifies git, not the write.** `_ensure_ccmem_gitignore` writes `*` then verifies with `git check-ignore -q` (must be ignored) and `git ls-files --error-unmatch` (must NOT be tracked → hard fail with `git rm --cached` guidance). Degrades when git is absent or the path isn't a work tree (nothing to leak); only fails when git confirms exposure.
3. **gate_injection_format's concern moved, not deleted.** Its checks (both markers, count line parseable, no partial memory lines, empty→stub) are ported into `gate_budget._check_file`, which already reads the generated files. The gate is still retired in Task 7.
4. **Scoping test pollution is a real bug** (Task 10). A transient nested `.git` in `gates/` makes `git rev-parse --git-common-dir` resolve to `gates` instead of the repo root. `scoping.py` itself is stateless (verified). Task 10 bisects the suite to find which test leaves that state and fixes the leak. Deferred past Task 3 per the review, but must not survive the plan.

**Deferred flag — `gate_phase1_notes` measures the wrong thing.** It was written for hook-based explicit capture over a week of dogfooding that never happened (hooks got blocked mid-way). It should be rewritten to assess the capture/generate workflow instead. Not changed here; flagged so it gets rewritten before Phase 1 is declared done, rather than sitting red forever or being quietly deleted.

## Global Constraints

- Python 3.11+, stdlib-first.
- Atomic write: PID-tagged `.tmp` in the target dir → `fsync` → `os.replace()`. Never write in place; clean up `.tmp` on failure.
- Deterministic output: given the same DB state, two `generate` calls produce byte-identical files. No `datetime.now()`, no age strings, no UUIDs, no `access_count` in ordering.
- Selection order (what to keep): `created_at DESC, id DESC` (recency). Render order (how it's written): `created_at ASC, id ASC`.
- Token cap applies to the **whole file** (header + count line + body + footer). Global = 400, project = 800.
- Always write the file, even for an empty DB — a stub with the count line `0 of 0`. @import always finds a file.
- `.ccmem/` protection via `.ccmem/.gitignore` = `*`, established before `memories.md` is written.

## Review Focus

1. **Ossification (the rev-2 bug #1):** seed past the cap, add one newer memory, regenerate — the new memory must appear and the oldest shown one must drop. Pinned to Task 1.
2. **Protection ordering (rev-2 bug #2):** if `.gitignore` can't be written, `memories.md` must not exist afterward. Pinned to Task 2 (simulate an unwritable `.ccmem/.gitignore`).
3. **Whole-file cap arithmetic:** the count line's digit count grows with the memory total; a file at the boundary must still measure ≤ cap *as written*, not just its body. Pinned to Task 1 (render-loop verifies the assembled string).
4. **Scope leakage:** `generate_global` must never emit project-scope rows regardless of DB contents. Pinned to Task 1.
5. **Empty-DB stub:** zero memories must still produce a valid, @import-safe file with both markers and a `0 of 0` count line. Pinned to Task 2.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `ccmem/generate.py` | CREATE | Rendering (two-phase), atomic write, gitignore-first, generate_global/project |
| `ccmem/cli.py` | MODIFY | Add `cmd_generate` + subparser; add @import health + truncation warning to `cmd_doctor` |
| `gates/gate_budget.py` | REWRITE | Assert whole generated files ≤ caps (not hook output) |
| `gates/gate_generate_determinism.py` | CREATE | Two-run byte-identical check |
| `gates/run_gates.py` | MODIFY | Register `gate_generate_determinism`; drop retired hook gates |
| `gates/gate_hook_contract.py` | DELETE | Tests blocked hooks (rev-2 #6) |
| `gates/gate_recovery_budget.py` | DELETE | Tests the hook-only recovery sweep (rev-2 #6) |
| `gates/gate_injection_format.py` | DELETE | Tests hook injection + old format (rev-2 #6) |
| `gates/gate_cache_safety.py` | DELETE | Tests hook determinism; superseded by gate_generate_determinism (rev-2 #6) |
| `tests/test_generate.py` | CREATE | Unit tests: ossification, caps, gitignore-first, stub, determinism |
| `tests/test_cli_robustness.py` | CREATE | R1 exit-0 + hostile-input discipline for CLI entrypoints (rev-2 #6) |
| `docs/FACTS.md` | MODIFY | Record why the hook gates were retired (rev-2 #6) |

---

## Task 1: `ccmem/generate.py` — two-phase render + core (rev-2 #1, #3, #4, #5)

**Files:**
- Create: `ccmem/generate.py`

**Interfaces:**
- Produces: `generate_global(con, claude_home=None) -> Path`, `generate_project(con, project_root, *, require_gitignore=True) -> Path`
- `_render(memories: list[Memory], max_tokens: int) -> str` — memories arrive in selection order (recency DESC); returns whole-file string ≤ max_tokens
- `_write_atomic(path: Path, content: str)` — raises OSError on failure, leaves no `.tmp`
- `_ensure_ccmem_gitignore(ccmem_dir: Path, *, require: bool)` — writes `.ccmem/.gitignore` = `*`, verifies; raises if `require` and it can't
- `GLOBAL_CAP = 400`, `PROJECT_CAP = 800`

```python
# ccmem/generate.py — implement exactly this
from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from ccmem.retrieval import Memory

GLOBAL_CAP = 400   # tokens — applies to the WHOLE FILE
PROJECT_CAP = 800  # tokens — applies to the WHOLE FILE

_HEADER = "<!-- ccmem -->"
_FOOTER = "<!-- /ccmem -->"


def _estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _mem_line(m: Memory) -> str:
    return f"- [{m.type}] {m.content}"


def _count_line(shown: int, total: int, cap: int) -> str:
    return f"<!-- ccmem: {shown} of {total} memories shown ({cap} token cap) -->"


def _assemble(render_order: list[Memory], shown: int, total: int, cap: int) -> str:
    parts = [_HEADER, _count_line(shown, total, cap)]
    body = "\n".join(_mem_line(m) for m in render_order)
    if body:
        parts.append(body)
    parts.append(_FOOTER)
    return "\n".join(parts) + "\n"


def _render(memories: list[Memory], max_tokens: int) -> str:
    """Two-phase, deterministic, whole-file cap.

    `memories` arrive in SELECTION order (recency DESC).
    Phase 1 (select): greedily keep the most-recent memories whose assembled
      whole-file size stays within max_tokens; drop least-recent when over.
    Phase 2 (render): emit the selected set sorted created_at ASC, id ASC.
    A count line ("N of M memories shown") is always present as a truncation signal.
    """
    total = len(memories)
    selected = list(memories)  # recency DESC; pop() drops the least-recent
    while True:
        shown = len(selected)
        render_order = sorted(selected, key=lambda m: (m.created_at, m.id))
        out = _assemble(render_order, shown, total, max_tokens)
        if _estimate_tokens(out) <= max_tokens or not selected:
            return out
        selected.pop()


def _write_atomic(path: Path, content: str) -> None:
    """Atomic write: PID-tagged .tmp in the target dir → fsync → os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _ensure_ccmem_gitignore(ccmem_dir: Path, *, require: bool) -> None:
    """Write .ccmem/.gitignore containing '*' (ignores the whole dir, incl. itself).

    Idempotent: skips the write if already exactly '*'. If `require` and protection
    cannot be established/verified, raises RuntimeError so the caller writes nothing.
    """
    gi = ccmem_dir / ".gitignore"
    try:
        if gi.exists() and gi.read_text(encoding="utf-8").strip() == "*":
            return
        _write_atomic(gi, "*\n")
        if gi.read_text(encoding="utf-8").strip() != "*":
            raise RuntimeError("verification read did not return '*'")
    except Exception as exc:
        if require:
            raise RuntimeError(
                f"Could not establish .ccmem/.gitignore protection at {gi}: {exc}\n"
                "Refusing to write memories.md unprotected. Fix directory permissions "
                "or pass require_gitignore=False for a throwaway location."
            ) from exc


def _fetch(con: sqlite3.Connection, scope_sql: str, params: tuple) -> list[Memory]:
    """Selection-order fetch: recency DESC. (Seam: prepend 'pinned DESC,' and,
    from Phase 2, 'rank,' ahead of created_at when those columns exist.)"""
    rows = con.execute(
        "SELECT id, type, content, subject, scope, created_at, access_count "
        "FROM memories "
        f"WHERE status='active' AND ({scope_sql}) "
        "ORDER BY created_at DESC, id DESC",
        params,
    ).fetchall()
    return [Memory(*r) for r in rows]


def generate_global(con: sqlite3.Connection, claude_home: str | Path | None = None) -> Path:
    """Write ~/.claude/ccmem-memories.md (global + user scope, ≤ GLOBAL_CAP tokens)."""
    if claude_home is None:
        claude_home = Path(os.path.expanduser("~")) / ".claude"
    claude_home = Path(claude_home)
    memories = _fetch(con, "scope='global' OR scope='user'", ())
    content = _render(memories, GLOBAL_CAP)
    path = claude_home / "ccmem-memories.md"
    _write_atomic(path, content)
    return path


def generate_project(
    con: sqlite3.Connection,
    project_root: str | Path,
    *,
    require_gitignore: bool = True,
) -> Path:
    """Write <project>/.ccmem/memories.md (project scope, ≤ PROJECT_CAP tokens).

    Establishes .ccmem/.gitignore protection FIRST; only then writes memories.md.
    """
    from ccmem.scoping import project_key
    root = Path(project_root)
    ccmem_dir = root / ".ccmem"
    _ensure_ccmem_gitignore(ccmem_dir, require=require_gitignore)  # protect BEFORE writing
    pid, _ = project_key(str(root))
    memories = _fetch(con, "scope='project' AND project_id=?", (pid,))
    content = _render(memories, PROJECT_CAP)
    path = ccmem_dir / "memories.md"
    _write_atomic(path, content)
    return path
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_generate.py
import os, sqlite3, tempfile
from pathlib import Path


def _seed(con, memories):
    from ccmem.db import migrate
    migrate(con)
    for m in memories:
        con.execute(
            "INSERT INTO memories "
            "(id, type, content, subject, scope, project_id, project_root, "
            "created_at, status, access_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            m,
        )
    con.commit()


def test_render_empty_returns_stub():
    from ccmem.generate import _render
    out = _render([], 400)
    assert out.startswith("<!-- ccmem -->")
    assert "0 of 0 memories shown" in out
    assert out.rstrip().endswith("<!-- /ccmem -->")


def test_render_no_age_strings():
    from ccmem.generate import _render
    from ccmem.retrieval import Memory
    mems = [Memory("id1", "decision", "chose X", None, "project", "2026-01-01T00:00:00Z", 0)]
    out = _render(mems, 400)
    assert "[decision]" in out
    # old format used "[type | age]"; new format has no " | " separator in lines
    assert "- [decision | " not in out


def test_render_whole_file_within_cap():
    from ccmem.generate import _render, _estimate_tokens, GLOBAL_CAP
    from ccmem.retrieval import Memory
    big = [Memory(f"id{i:03d}", "decision", "x" * 80, None, "global",
                  f"2026-01-{(i % 28) + 1:02d}T00:00:00Z", 0) for i in range(60)]
    out = _render(big, GLOBAL_CAP)
    assert _estimate_tokens(out) <= GLOBAL_CAP  # WHOLE FILE, incl header/footer/count


def test_render_no_partial_line():
    from ccmem.generate import _render, GLOBAL_CAP
    from ccmem.retrieval import Memory
    big = [Memory(f"id{i:03d}", "decision", f"memory number {i} " + "y" * 60, None,
                  "global", f"2026-01-{(i % 28) + 1:02d}T00:00:00Z", 0) for i in range(60)]
    out = _render(big, GLOBAL_CAP)
    for line in out.splitlines():
        if line.startswith("- ["):
            assert line.startswith("- [decision] memory number")  # never truncated mid-line


def test_render_recency_selection_then_created_asc_render():
    """Rev-2 #1: newest memories are selected; the rendered set is created_at ASC."""
    from ccmem.generate import _render
    from ccmem.retrieval import Memory
    # 3 memories, tiny cap fits ~2. Newest two (id b @ 03, id c @ 02) should win
    # over the oldest (id a @ 01). Rendered order must be ascending by created_at.
    mems_recency_desc = [
        Memory("c", "decision", "CCC", None, "global", "2026-01-03T00:00:00Z", 0),
        Memory("b", "decision", "BBB", None, "global", "2026-01-02T00:00:00Z", 0),
        Memory("a", "decision", "AAA", None, "global", "2026-01-01T00:00:00Z", 0),
    ]
    # cap chosen so overhead + 2 lines fit but not 3
    out = _render(mems_recency_desc, 30)
    assert "AAA" not in out          # oldest dropped
    assert "BBB" in out and "CCC" in out
    assert out.index("BBB") < out.index("CCC")  # render order created_at ASC


def test_generate_global_excludes_project_scope():
    from ccmem.generate import generate_global
    import hashlib
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        pid = hashlib.sha256(b"/proj").hexdigest()[:16]
        _seed(con, [
            ("g1", "decision", "global fact", None, "global", pid, "/proj", "2026-01-01T00:00:00Z", "active", 0),
            ("p1", "decision", "project fact", None, "project", pid, "/proj", "2026-01-02T00:00:00Z", "active", 0),
        ])
        text = generate_global(con, claude_home=Path(tmp) / "claude").read_text(encoding="utf-8")
        assert "global fact" in text and "project fact" not in text
        con.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_generate.py -v`
Expected: `ModuleNotFoundError: No module named 'ccmem.generate'`

- [ ] **Step 3: Create `ccmem/generate.py`** with the full implementation above.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_generate.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add ccmem/generate.py tests/test_generate.py
git commit -m "$(cat <<'EOF'
feat(generate): two-phase render — recency selection, created_at render, whole-file cap

Selection keeps the most-recent memories that fit; render sorts created_at ASC.
Fixes rank ossification (oldest memories no longer freeze the file). Cap counts
header + count line + footer. Truncation is signalled by an in-block count line.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: gitignore-first, atomic write, empty-stub (rev-2 #2, #3)

**Files:**
- Modify: `tests/test_generate.py` (append)

**Interfaces:**
- Consumes: `generate_project(con, project_root, *, require_gitignore=True) -> Path`, `_ensure_ccmem_gitignore`, `_write_atomic`

- [ ] **Step 1: Write the failing tests (append)**

```python
def test_generate_project_gitignore_is_self_contained():
    from ccmem.generate import generate_project
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        _seed(con, [])
        generate_project(con, tmp, require_gitignore=True)
        gi = Path(tmp) / ".ccmem" / ".gitignore"
        assert gi.exists()
        assert gi.read_text(encoding="utf-8").strip() == "*"
        # repo-level .gitignore must NOT be touched
        assert not (Path(tmp) / ".gitignore").exists()
        con.close()


def test_generate_project_gitignore_idempotent():
    from ccmem.generate import generate_project
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        _seed(con, [])
        generate_project(con, tmp)
        generate_project(con, tmp)  # second run must not duplicate or error
        gi = Path(tmp) / ".ccmem" / ".gitignore"
        assert gi.read_text(encoding="utf-8").strip() == "*"
        con.close()


def test_generate_project_writes_memories_after_protection():
    from ccmem.generate import generate_project
    import hashlib
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        pid = hashlib.sha256(tmp.encode()).hexdigest()[:16]
        _seed(con, [
            ("p1", "gotcha", "port 8076 closed", None, "project", pid, tmp, "2026-01-01T00:00:00Z", "active", 0),
        ])
        path = generate_project(con, tmp)
        assert path.exists()
        assert "port 8076 closed" in path.read_text(encoding="utf-8")
        con.close()


def test_generate_project_no_memories_file_when_protection_fails():
    """Rev-2 #2: if .gitignore can't be established, memories.md must not exist."""
    from ccmem.generate import generate_project
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        _seed(con, [])
        # Make .ccmem a FILE, so mkdir/.gitignore write fails.
        ccmem_path = Path(tmp) / ".ccmem"
        ccmem_path.write_text("blocker", encoding="utf-8")
        import pytest
        with pytest.raises(RuntimeError):
            generate_project(con, tmp, require_gitignore=True)
        # memories.md must not exist (protection failed before any memory write)
        assert not (ccmem_path / "memories.md").exists() if ccmem_path.is_dir() else True
        con.close()


def test_generate_project_empty_db_writes_stub():
    from ccmem.generate import generate_project
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        _seed(con, [])
        path = generate_project(con, tmp)
        text = path.read_text(encoding="utf-8")
        assert "<!-- ccmem -->" in text and "<!-- /ccmem -->" in text
        assert "0 of 0 memories shown" in text
        con.close()


def test_write_atomic_leaves_no_tmp():
    from ccmem.generate import _write_atomic
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.md"
        _write_atomic(path, "hello\n")
        assert path.read_text(encoding="utf-8") == "hello\n"
        assert list(Path(tmp).glob("*.tmp")) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_generate.py -v -k "gitignore or protection or stub or atomic"`
Expected: failures — `generate_project` gitignore-first path not yet implemented (Task 1 already added the function, so these validate its behavior; if any fail, fix `ccmem/generate.py`).

- [ ] **Step 3: Confirm `ccmem/generate.py` matches the Task 1 implementation** (gitignore established before memory write). Adjust if a test fails.

- [ ] **Step 4: Run all generate tests**

Run: `python -m pytest tests/test_generate.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add tests/test_generate.py ccmem/generate.py
git commit -m "$(cat <<'EOF'
test(generate): gitignore-first protection, self-contained .ccmem/.gitignore, empty stub

.ccmem/.gitignore='*' protects the directory before memories.md is written; if
protection fails, nothing is written. Repo-level .gitignore is never touched.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

> **PAUSE HERE.** Per the review, show the user Tasks 1 and 2 (the actual `ccmem/generate.py` and the test results) before proceeding to Task 3.

---

## Task 3: Determinism test

**Files:**
- Modify: `tests/test_generate.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_generate_deterministic_across_access_count_mutation():
    from ccmem.generate import generate_global, generate_project
    import hashlib
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        pid = hashlib.sha256(tmp.encode()).hexdigest()[:16]
        _seed(con, [
            ("g1", "decision", "global A", None, "global", pid, tmp, "2026-01-01T00:00:00Z", "active", 5),
            ("g2", "preference", "global B", None, "user", pid, tmp, "2026-01-02T00:00:00Z", "active", 2),
            ("p1", "gotcha", "project C", None, "project", pid, tmp, "2026-01-03T00:00:00Z", "active", 10),
            ("p2", "correction", "project D", None, "project", pid, tmp, "2026-01-04T00:00:00Z", "active", 0),
        ])
        claude = Path(tmp) / "claude"
        g1 = generate_global(con, claude_home=claude).read_text(encoding="utf-8")
        p1 = generate_project(con, tmp).read_text(encoding="utf-8")
        con.execute("UPDATE memories SET access_count = access_count + 1")
        con.commit()
        g2 = generate_global(con, claude_home=claude).read_text(encoding="utf-8")
        p2 = generate_project(con, tmp).read_text(encoding="utf-8")
        assert g1 == g2
        assert p1 == p2
        con.close()
```

- [ ] **Step 2: Run to verify it passes** (ordering already deterministic)

Run: `python -m pytest tests/test_generate.py::test_generate_deterministic_across_access_count_mutation -v`
Expected: PASS. If it fails, the render path has a volatile value — fix it.

- [ ] **Step 3: Commit**

```bash
git add tests/test_generate.py
git commit -m "$(cat <<'EOF'
test(generate): determinism across access_count mutation

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `ccmem generate` CLI subcommand

**Files:**
- Modify: `ccmem/cli.py`

**Interfaces:**
- Consumes: `generate_global`, `generate_project`, `GLOBAL_CAP`, `PROJECT_CAP` from `ccmem.generate`; `resolve_project_root` from `ccmem.scoping`

```python
# Add to ccmem/cli.py

def cmd_generate(args):
    from ccmem.generate import GLOBAL_CAP, PROJECT_CAP, generate_global, generate_project
    from ccmem.scoping import resolve_project_root
    con = _db(args)
    project_root = getattr(args, "project_root", None) or resolve_project_root(os.getcwd())
    project_only = getattr(args, "project_only", False)
    global_only = getattr(args, "global_only", False)

    if not project_only:
        path = generate_global(con, claude_home=Path(os.path.expanduser("~")) / ".claude")
        tokens = (len(path.read_text(encoding="utf-8")) + 3) // 4
        print(f"global:  {path}  (~{tokens} / {GLOBAL_CAP} tokens)")

    if not global_only:
        path = generate_project(con, project_root)
        tokens = (len(path.read_text(encoding="utf-8")) + 3) // 4
        print(f"project: {path}  (~{tokens} / {PROJECT_CAP} tokens)")

    con.close()
```

Subparser in `main()`:
```python
gen = sub.add_parser("generate", help="write memory markdown files for @import")
gen.add_argument("--global-only", action="store_true")
gen.add_argument("--project-only", action="store_true")
gen.add_argument("--project-root")
```

Dispatch entry: `"generate": cmd_generate,`

- [ ] **Step 1: Write the failing test (append to tests/test_generate.py)**

```python
def test_cli_generate_global_only_runs():
    import subprocess, sys
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "mem.db"))
        _seed(con, [
            ("g1", "decision", "global fact", None, "global", "p", "/x", "2026-01-01T00:00:00Z", "active", 0),
        ])
        con.close()
        env = {**os.environ, "CCMEM_HOME": tmp}
        result = subprocess.run(
            [sys.executable, "-m", "ccmem.cli", "generate", "--global-only"],
            env=env, capture_output=True, text=True, cwd=tmp,
        )
        assert result.returncode == 0, result.stderr
        assert "global:" in result.stdout
```

- [ ] **Step 2: Run to verify it fails** — `python -m pytest tests/test_generate.py::test_cli_generate_global_only_runs -v` → SystemExit / arg error.

- [ ] **Step 3: Add `cmd_generate`, subparser, and dispatch entry to `ccmem/cli.py`.**

- [ ] **Step 4: Run** — `python -m pytest tests/test_generate.py -v` → all pass.

- [ ] **Step 5: Manual smoke** — `python -m ccmem.cli generate` → prints two paths + token counts; files exist.

- [ ] **Step 6: Commit**

```bash
git add ccmem/cli.py tests/test_generate.py
git commit -m "$(cat <<'EOF'
feat(cli): ccmem generate subcommand

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Rewrite `gates/gate_budget.py` — whole-file assertion (rev-2 #4)

**Files:**
- Modify: `gates/gate_budget.py` (full rewrite)

**Interfaces:**
- Consumes: `ccmem.generate.generate_global/project`, `_common.{REPO_ROOT, GateResult, estimate_tokens}`

```python
#!/usr/bin/env python3
"""Gate: generated-file token budget (DESIGN caps: global=400, project=800).

The cap applies to the WHOLE FILE as written — header, count line, memory body,
and footer all count. This gate seeds far more memories than either cap allows,
generates the files, and asserts the file-as-written stays within budget.

Run: python gates/gate_budget.py
"""
from __future__ import annotations
import hashlib, os, sys, tempfile
from pathlib import Path
from _common import REPO_ROOT, GateResult, estimate_tokens

SEED_ROWS = 400
GLOBAL_CAP = 400
PROJECT_CAP = 800


def seed_mixed(db_path: str, project_root: str, n: int) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from ccmem.db import connect, migrate
    pid = hashlib.sha256(project_root.encode()).hexdigest()[:16]
    con = connect(db_path)
    migrate(con)
    for i in range(n):
        scope = "global" if i % 3 == 0 else ("user" if i % 3 == 1 else "project")
        con.execute(
            "INSERT OR IGNORE INTO memories "
            "(id, type, content, subject, scope, project_id, project_root, "
            "created_at, status, access_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"b-{i:04d}", "decision",
             f"Budget memory {i}: replacing legacy component {i % 7} with the new one.",
             f"subj-{i}", scope, pid, project_root,
             f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T00:00:00Z", "active", i),
        )
    con.commit()
    con.close()


import re

_COUNT_RE = re.compile(r"<!-- ccmem: (\d+) of (\d+) memories shown \(\d+ token cap\) -->")
_MEM_LINE_RE = re.compile(r"^- \[\w+\] .+$")  # well-formed memory line; no partial lines


def _check_file(r: GateResult, label: str, path: Path, cap: int) -> None:
    # Ported from the retired gate_injection_format: the injected block must be
    # well-formed — both markers present, count line parseable, no partial memory
    # lines. The concern outlived the gate; it now rides on the generated file.
    if not path.exists():
        r.fail(f"{label} written", f"missing: {path}")
        return
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        r.fail(f"{label} non-empty", "seeded DB but file is blank")
        return
    r.ok(f"{label} written", f"{len(text)} chars")
    toks = estimate_tokens(text)  # WHOLE FILE
    if toks > cap:
        r.fail(f"{label} within whole-file cap", f"~{toks} > {cap}")
    else:
        r.ok(f"{label} within whole-file cap", f"~{toks} <= {cap}")
    if "<!-- ccmem -->" not in text or "<!-- /ccmem -->" not in text:
        r.fail(f"{label} delimited", "missing <!-- ccmem --> / <!-- /ccmem -->")
    else:
        r.ok(f"{label} delimited")
    m = _COUNT_RE.search(text)
    if not m:
        r.fail(f"{label} count line parseable", "no '<!-- ccmem: N of M memories shown (C token cap) -->'")
    else:
        r.ok(f"{label} count line parseable", f"{m.group(1)} of {m.group(2)}")
    bad = [ln for ln in text.splitlines() if ln.startswith("- [") and not _MEM_LINE_RE.match(ln)]
    if bad:
        r.fail(f"{label} no partial memory lines", f"malformed: {bad[0]!r}")
    else:
        r.ok(f"{label} no partial memory lines")


def main() -> int:
    r = GateResult("generated-file budget")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "mem.db")
        proj = os.path.join(tmp, "proj")
        os.makedirs(proj, exist_ok=True)
        seed_mixed(db_path, proj, SEED_ROWS)
        r.ok("test DB seeded", f"{SEED_ROWS} rows (global/user/project)")

        sys.path.insert(0, str(REPO_ROOT))
        try:
            from ccmem.db import connect, migrate
            from ccmem.generate import generate_global, generate_project
        except Exception as exc:
            r.fail("ccmem.generate importable", str(exc))
            return r.report()

        con = connect(db_path)
        g = generate_global(con, claude_home=Path(tmp) / "claude")
        _check_file(r, "global file", g, GLOBAL_CAP)
        p = generate_project(con, proj, require_gitignore=False)
        _check_file(r, "project file", p, PROJECT_CAP)

        # graceful degradation: empty DB still writes a valid stub
        empty = os.path.join(tmp, "empty.db")
        econ = connect(empty); migrate(econ)
        estub = generate_global(econ, claude_home=Path(tmp) / "empty_claude")
        etext = estub.read_text(encoding="utf-8")
        if "<!-- ccmem -->" in etext and "0 of 0 memories shown" in etext:
            r.ok("empty DB writes stub")
        else:
            r.fail("empty DB writes stub", "stub markers/count line missing")
        econ.close(); con.close()
    return r.report()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 1: Replace `gates/gate_budget.py`** with the version above.
- [ ] **Step 2: Run** — `python gates/gate_budget.py` → all checks pass.
- [ ] **Step 3: Commit**

```bash
git add gates/gate_budget.py
git commit -m "$(cat <<'EOF'
refactor(gate_budget): assert whole generated file within cap, not hook output

Cap now covers header + count line + body + footer, measured on the file as
written by ccmem generate. Replaces the SessionStart-hook-driven check.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: New `gates/gate_generate_determinism.py`

**Files:**
- Create: `gates/gate_generate_determinism.py`
- Modify: `gates/run_gates.py` (REGISTRY)

```python
#!/usr/bin/env python3
"""Gate: generate output is byte-identical across two calls with the same DB state.

The generated files land in the SessionStart prompt prefix via @import. If they
differ between otherwise-identical runs, every session pays a cache write. Two
generate calls — even with access_count mutated between them — must be identical.

Run: python gates/gate_generate_determinism.py
"""
from __future__ import annotations
import hashlib, os, sys, tempfile
from pathlib import Path
from _common import REPO_ROOT, GateResult

SEED_N = 30


def _seed(db_path: str, project_root: str) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from ccmem.db import connect, migrate
    pid = hashlib.sha256(project_root.encode()).hexdigest()[:16]
    con = connect(db_path); migrate(con)
    for i in range(SEED_N):
        scope = "global" if i % 3 == 0 else ("user" if i % 3 == 1 else "project")
        con.execute(
            "INSERT OR IGNORE INTO memories "
            "(id, type, content, subject, scope, project_id, project_root, "
            "created_at, status, access_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"d-{i:03d}", "decision", f"Determinism seed {i}: approach {i % 4}.",
             f"s-{i}", scope, pid, project_root,
             f"2026-01-{(i % 28) + 1:02d}T00:00:00Z", "active", i * 3),
        )
    con.commit(); con.close()


def _check(r: GateResult, label: str, a: str, b: str) -> None:
    if a == b:
        r.ok(f"{label}: byte-identical across 2 runs", f"{len(a)} chars")
        return
    idx = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    lo, hi = max(0, idx - 40), idx + 40
    r.fail(f"{label}: byte-identical across 2 runs",
           f"diverges at char {idx}: {a[lo:hi]!r} vs {b[lo:hi]!r}")


def main() -> int:
    r = GateResult("generate determinism")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "mem.db")
        proj = os.path.join(tmp, "proj"); os.makedirs(proj, exist_ok=True)
        _seed(db_path, proj)
        r.ok("test DB seeded", f"{SEED_N} rows")

        sys.path.insert(0, str(REPO_ROOT))
        try:
            from ccmem.db import connect
            from ccmem.generate import generate_global, generate_project
        except Exception as exc:
            r.fail("ccmem.generate importable", str(exc)); return r.report()

        claude = Path(tmp) / "claude"
        con = connect(db_path)
        g1 = generate_global(con, claude_home=claude).read_text(encoding="utf-8")
        p1 = generate_project(con, proj, require_gitignore=False).read_text(encoding="utf-8")
        if not g1.strip():
            r.fail("run1 non-empty", "empty output proves nothing"); return r.report()
        con.execute("UPDATE memories SET access_count = access_count + 1"); con.commit()
        g2 = generate_global(con, claude_home=claude).read_text(encoding="utf-8")
        p2 = generate_project(con, proj, require_gitignore=False).read_text(encoding="utf-8")
        _check(r, "global file", g1, g2)
        _check(r, "project file", p1, p2)
        con.close()
    return r.report()


if __name__ == "__main__":
    sys.exit(main())
```

REGISTRY edit (after `gate_budget`):
```python
("gate_generate_determinism", 1, "generate output is byte-identical across repeated calls"),
```

- [ ] **Step 1: Create the gate.**
- [ ] **Step 2: Run** — `python gates/gate_generate_determinism.py` → all pass.
- [ ] **Step 3: Add to REGISTRY** in `run_gates.py`.
- [ ] **Step 4: Commit**

```bash
git add gates/gate_generate_determinism.py gates/run_gates.py
git commit -m "$(cat <<'EOF'
feat(gates): gate_generate_determinism — byte-stable @import files

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Retire dead hook gates; preserve discipline in CLI tests (rev-2 #6)

**Rationale:** `allowManagedHooksOnly: true` (<org> org policy) permanently blocks all ccmem hooks in this environment. Gates that drive hooks via `run_hook` test code that Claude Code will never invoke.

**Measured baseline (2026-09-26, `run_gates.py --phase 1`):** of the four hook gates, only `gate_hook_contract` is red today (Defender-driven timing on the kill-switch/budget checks). `gate_cache_safety`, `gate_recovery_budget`, and `gate_injection_format` are green — but they exercise blocked-hook code paths, and `gate_injection_format` specifically validates the *old* `<ccmem-memories>` / `[type | age]` format that `generate` abandons. Keeping them green is false confidence: a passing gate over a path Claude Code never runs. Retire all four.

**Side-effect fix:** `gate_scaffold` is also red today, but *only* because the hook gates run with `CCMEM_HOME=REPO_ROOT/.ccmem-test` and leave that directory behind, tripping scaffold's "`.ccmem-test/` absent at rest" check. Retiring the hook gates removes the sole source of that pollution; this task also deletes the stale directory once, after which `gate_scaffold` goes green on its own.

**Out of scope:** `gate_phase1_notes` is red because `docs/PHASE1-NOTES.md` doesn't exist yet — that's the week-of-dogfooding exit doc, not something this feature writes. Phase 1 will show `gate_phase1_notes` red until that doc is authored; every other phase-1 gate goes green after this task.

Move the parts worth keeping (R1 exit-0, hostile-input survival) to a test of the CLI entrypoints that now do the capture/generate work.

**Files:**
- Delete: `gates/gate_hook_contract.py`, `gates/gate_recovery_budget.py`, `gates/gate_injection_format.py`, `gates/gate_cache_safety.py`
- Modify: `gates/run_gates.py` (remove the four from REGISTRY)
- Create: `tests/test_cli_robustness.py`
- Modify: `docs/FACTS.md` (record the retirement + reasoning)

- [ ] **Step 1: Delete the stale `.ccmem-test/` left by the hook gates**

Run: `rm -rf .ccmem-test` (this is the artifact tripping `gate_scaffold`; the hook gates that recreate it are being retired in this same task, so it stays gone).

- [ ] **Step 2: Write `tests/test_cli_robustness.py`** — port the hostile-input corpus from `gate_hook_contract.py` and the R1 exit-0 discipline, aimed at the CLI entrypoints.

```python
import os, subprocess, sys, tempfile

HOSTILE_TRANSCRIPTS = {
    "empty file": b"",
    "not json": b"this is not json at all\n",
    "truncated json": b'{"type":"user","message"\n',
    "null line": b"null\n",
    "array line": b"[]\n",
    "wrong types": b'{"type":42,"message":null}\n',
}


def _run(args, env=None, cwd=None):
    return subprocess.run([sys.executable, "-m", "ccmem.cli", *args],
                          capture_output=True, text=True, env=env, cwd=cwd)


def test_capture_survives_hostile_transcripts():
    """R1: ccmem capture must exit 0 (never crash the caller) on garbage input."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "CCMEM_HOME": tmp}
        for label, blob in HOSTILE_TRANSCRIPTS.items():
            t = os.path.join(tmp, "t.jsonl")
            with open(t, "wb") as f:
                f.write(blob)
            r = _run(["capture", t, "--session-id", "hostile"], env=env, cwd=tmp)
            assert r.returncode == 0, f"{label}: rc={r.returncode} stderr={r.stderr[:200]}"


def test_capture_missing_file_exits_zero():
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "CCMEM_HOME": tmp}
        r = _run(["capture", os.path.join(tmp, "nope.jsonl"), "--session-id", "x"], env=env, cwd=tmp)
        assert r.returncode == 0, r.stderr


def test_generate_on_empty_db_exits_zero():
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "CCMEM_HOME": tmp}
        r = _run(["generate", "--global-only"], env=env, cwd=tmp)
        assert r.returncode == 0, r.stderr
```

If `ccmem capture` does not already exit 0 on hostile input, fix `cmd_capture` to guard its transcript reads (R1). Re-run until green.

- [ ] **Step 3: Run the new tests** — `python -m pytest tests/test_cli_robustness.py -v` → all pass (fix `cmd_capture` guards if needed).

- [ ] **Step 4: Delete the four gate files and remove them from REGISTRY** in `run_gates.py`.

- [ ] **Step 5: Record the retirement in `docs/FACTS.md`** — a dated entry naming the four gates, the `allowManagedHooksOnly` cause, and where the discipline moved (`tests/test_cli_robustness.py`).

- [ ] **Step 6: Verify phase 1** — `python gates/run_gates.py --phase 1` → every gate passes **except `gate_phase1_notes`** (expected: the dogfooding doc isn't written yet — out of scope). `gate_scaffold` must now be green (stale `.ccmem-test/` gone). If any *other* gate is red, stop and report — do not fold it into this commit.

- [ ] **Step 7: Commit (its own commit, per the review)**

```bash
git add gates/run_gates.py tests/test_cli_robustness.py docs/FACTS.md
git rm gates/gate_hook_contract.py gates/gate_recovery_budget.py gates/gate_injection_format.py gates/gate_cache_safety.py
git commit -m "$(cat <<'EOF'
chore(gates): retire hook-dependent gates; move exit-0 discipline to CLI tests

allowManagedHooksOnly blocks all ccmem hooks in this environment, so gates that
drive hooks test never-invoked code and are permanently red on Defender timing.
Retired: gate_hook_contract, gate_recovery_budget, gate_injection_format,
gate_cache_safety. R1 exit-0 and hostile-input survival now covered by
tests/test_cli_robustness.py against the CLI entrypoints. Reasoning in FACTS.md.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Doctor — @import health + truncation warning (rev-2 #5 doctor half)

**Files:**
- Modify: `ccmem/cli.py` (append to `cmd_doctor`)

Doctor reports, for both tiers: whether the generated file exists, whether the owning `CLAUDE.md` contains the `@import` line, and — parsing the file's count line — a **warning when `shown < total`** (memories are being dropped: prune or raise the cap).

```python
    # --- Option E @import health (append to cmd_doctor) ---
    import re as _re
    from ccmem.scoping import resolve_project_root

    def _report_tier(mem_file: Path, claude_md: Path, import_line: str, gen_cmd: str):
        if mem_file.exists():
            txt = mem_file.read_text(encoding="utf-8")
            toks = (len(txt) + 3) // 4
            print(f"  file OK:   {mem_file}  (~{toks} tokens)")
            m = _re.search(r"<!-- ccmem: (\d+) of (\d+) memories shown", txt)
            if m:
                shown, total = int(m.group(1)), int(m.group(2))
                if shown < total:
                    print(f"  WARN: only {shown} of {total} memories shown — "
                          f"prune old memories or raise the cap ({gen_cmd}).")
        else:
            print(f"  file MISSING: {mem_file}  → run: {gen_cmd}")
        if claude_md.exists():
            if import_line in claude_md.read_text(encoding="utf-8"):
                print(f"  @import OK: {import_line} present in {claude_md}")
            else:
                print(f"  @import MISSING: add '{import_line}' to {claude_md}")
        else:
            print(f"  {claude_md} not found (create it to enable @import)")

    print("\nOption E @import health:")
    _claude_home = Path(os.path.expanduser("~")) / ".claude"
    _report_tier(_claude_home / "ccmem-memories.md", _claude_home / "CLAUDE.md",
                 "@ccmem-memories.md", "python -m ccmem.cli generate --global-only")
    _proj = resolve_project_root(os.getcwd())
    _report_tier(Path(_proj) / ".ccmem" / "memories.md", Path(_proj) / "CLAUDE.md",
                 "@.ccmem/memories.md", "python -m ccmem.cli generate --project-only")
```

- [ ] **Step 1: Append the block to `cmd_doctor`.**
- [ ] **Step 2: Run** — `python -m ccmem.cli generate && python -m ccmem.cli doctor` → "Option E @import health" section prints; files OK; @import lines reported MISSING until added manually.
- [ ] **Step 3: Test the truncation warning** — seed >cap memories, generate, doctor → shows the `WARN: only N of M` line.
- [ ] **Step 4: Commit**

```bash
git add ccmem/cli.py
git commit -m "$(cat <<'EOF'
feat(doctor): Option E @import health + truncation warning

Reports generated-file presence, missing @import lines, and warns when the
count line shows shown < total (memories dropped at the cap).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Final verification + report

- [ ] **Step 1:** `python -m pytest tests/test_generate.py tests/test_cli_robustness.py -v` → all pass.
- [ ] **Step 2:** `python -m pytest tests/ -q` → no new regressions.
- [ ] **Step 3:** `python gates/run_gates.py --phase 1` → every gate green **except `gate_phase1_notes`** (out of scope: needs `docs/PHASE1-NOTES.md`, written during real dogfooding).
- [ ] **Step 4:** `python -m ccmem.cli generate` against the real DB; inspect both files (within caps, real memories, count line present).
- [ ] **Step 5: Report to the user before wiring @import into real CLAUDE.md files:**
  - Global cap: 400 tokens (`~/.claude/ccmem-memories.md`); project cap: 800 tokens (`<project>/.ccmem/memories.md`).
  - Absent-file behavior: generate always writes a file (empty DB → `0 of 0` stub), so @import never points at nothing.
  - Then await the decision on adding `@ccmem-memories.md` and `@.ccmem/memories.md`.

---

## Task 10: Fix scoping test order-pollution (rev-3 #4)

**Symptom:** `tests/test_scoping.py::test_resolve_returns_repo_root_for_subdir` passes in isolation but fails in the full suite, returning `<repo>/gates` instead of `<repo>`. Cause: `git rev-parse --git-common-dir` run from `gates/` returns a bare `.git` (relative), which only happens when a nested `.git` transiently exists in `gates/`. `scoping.py` is stateless — the state is created by some other test.

**Files:**
- Investigate: `tests/` (find the test that creates a git repo or `.git` at/under `gates/`, or leaks a `cwd`/`os.chdir`)
- Fix: whichever test owns the leak (scope its git repo to a `tmp_path`, restore cwd, or stop writing under the repo tree)

- [ ] **Step 1: Reproduce and bisect.** Run `python -m pytest tests/test_scoping.py::test_resolve_returns_repo_root_for_subdir tests/<suspect>.py -q` pairing the scoping test after each hook/recovery test module until it fails. The hook tests spawn `hooks/*.py` subprocesses and write to `REPO/.ccmem-test`; a capture/recovery path that runs `git` with an unintended cwd is the prime suspect.
- [ ] **Step 2: Identify the exact shared state** — nested `.git` under `gates/`, a reused tmpdir, a real `~/.claude` read, or an unrestored `os.chdir`. Name it in the commit.
- [ ] **Step 3: Fix the leak at its source** (isolate to `tmp_path`, restore cwd in a fixture, etc.). Do not weaken the scoping assertion to paper over it.
- [ ] **Step 4: Verify** — run the full suite twice; `test_resolve_returns_repo_root_for_subdir` passes both times, and no `.ccmem-test/` or stray `.git` is left under the repo.
- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "$(cat <<'EOF'
fix(tests): stop <culprit> leaking git state into gates/ (scoping test flake)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-review against the six changes

1. **Ossification** → `_fetch` selects recency DESC, `_render` renders created_at ASC; `test_render_recency_selection_then_created_asc_render` proves new displaces old. ✅
2. **Gitignore-before-memories** → `generate_project` calls `_ensure_ccmem_gitignore(require=...)` first; `test_generate_project_no_memories_file_when_protection_fails` proves nothing is written on failure. ✅
3. **Self-contained `.ccmem/.gitignore`=`*`** → repo `.gitignore` untouched (asserted); no CRLF handling; idempotent. ✅
4. **Whole-file cap** → `_render` verifies the assembled string ≤ cap; `gate_budget` measures the file as written. ✅
5. **Visible truncation** → count line always emitted (deterministic); doctor warns when shown < total. ✅
6. **Retire dead gates** → Task 7 deletes four gates in their own commit, records reasoning in FACTS.md, moves R1/hostile-input to `tests/test_cli_robustness.py`; phase 1 goes green. ✅

**Placeholder scan:** none. **Type consistency:** `generate_global(con, claude_home=None)`, `generate_project(con, project_root, *, require_gitignore=True)` used identically across tasks and gates.
