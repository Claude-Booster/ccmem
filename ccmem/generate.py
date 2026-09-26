from __future__ import annotations
import os
import sqlite3
import subprocess
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
      whole-file size stays within max_tokens; drop the least-recent when over.
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


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess | None:
    """Run a git command in cwd. Returns the result, or None if git is unavailable."""
    try:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _ensure_ccmem_gitignore(ccmem_dir: Path, project_root: Path, *, require: bool) -> None:
    """Protect .ccmem/ with a self-contained .gitignore='*', then VERIFY via git.

    The write is the mechanism; git is the source of truth. Writing '*' does not
    prove git ignores anything — the file could already be tracked, or a parent
    rule could negate it. So after writing, verify:
      - `git ls-files --error-unmatch .ccmem/memories.md` succeeding means the file
        is TRACKED (gitignore does not untrack) — hard failure with rm --cached.
      - `git check-ignore -q .ccmem/memories.md` must succeed (the path is ignored).
    Degrades: if git is absent or project_root is not a work tree, there is nothing
    to leak, so proceed. Only fails when git confirms the path is exposed.
    """
    gi = ccmem_dir / ".gitignore"
    rel = ".ccmem/memories.md"

    # 1. Establish the mechanism.
    try:
        if not (gi.exists() and gi.read_text(encoding="utf-8").strip() == "*"):
            _write_atomic(gi, "*\n")
    except Exception as exc:
        if require:
            raise RuntimeError(
                f"Could not write .ccmem/.gitignore at {gi}: {exc}\n"
                "Refusing to write memories.md unprotected."
            ) from exc
        return

    # 2. Verify git's actual behaviour.
    inside = _git(["rev-parse", "--is-inside-work-tree"], project_root)
    if inside is None or inside.returncode != 0 or inside.stdout.strip() != "true":
        return  # no git, or not a repo — nothing to leak

    tracked = _git(["ls-files", "--error-unmatch", rel], project_root)
    if tracked is not None and tracked.returncode == 0:
        if require:
            raise RuntimeError(
                f"{rel} is already tracked by git — .gitignore will NOT untrack it.\n"
                f"Run: git -C {project_root} rm --cached {rel}\n"
                "then re-run ccmem generate."
            )
        return

    ignored = _git(["check-ignore", "-q", rel], project_root)
    if ignored is not None and ignored.returncode != 0:
        if require:
            raise RuntimeError(
                f"git does not ignore {rel} despite .ccmem/.gitignore — a parent "
                f"gitignore rule may negate it.\n"
                f"Check: git -C {project_root} check-ignore -v {rel}"
            )
        return


def _fetch(con: sqlite3.Connection, scope_sql: str, params: tuple) -> list[Memory]:
    """Selection-order fetch: pinned first, then recency.

    ORDER BY pinned DESC, created_at DESC, id DESC. Pinned memories survive
    truncation over newer unpinned ones. (Seam: insert 'rank,' after 'pinned DESC,'
    once Phase 2 embedding scoring exists.) Render order is applied separately in
    _render (created_at ASC, id ASC)."""
    rows = con.execute(
        "SELECT id, type, content, subject, scope, created_at, access_count "
        "FROM memories "
        f"WHERE status='active' AND ({scope_sql}) "
        "ORDER BY pinned DESC, created_at DESC, id DESC",
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
    _ensure_ccmem_gitignore(ccmem_dir, root, require=require_gitignore)  # protect BEFORE writing
    pid, _ = project_key(str(root))
    memories = _fetch(con, "scope='project' AND project_id=?", (pid,))
    content = _render(memories, PROJECT_CAP)
    path = ccmem_dir / "memories.md"
    _write_atomic(path, content)
    return path
