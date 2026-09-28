from __future__ import annotations
import hashlib
import os
import subprocess


class ScopingError(RuntimeError):
    """The git repo root could not be resolved for a reason OTHER than 'this
    directory is simply not in a git repository'.

    Callers must treat this loudly. Silently falling back to cwd is how the
    WinError-6 bug mis-scoped memories to the wrong project: a wrong answer that
    looked like a right one.
    """


def resolve_project_root(cwd: str) -> str:
    """Return the git repo root for `cwd` (worktree-safe).

    Returns `cwd` ONLY when `cwd` is definitively not inside a git repository
    (git ran and said so). Every other failure — git missing, timeout, spawn
    error, empty/garbled output, or an unexpected non-zero exit — raises
    ScopingError rather than guessing.
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,  # avoid inheriting an invalid stdin handle (WinError 6)
        )
    except FileNotFoundError as exc:
        raise ScopingError(f"git not found while resolving project root for {cwd!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScopingError(f"git timed out while resolving project root for {cwd!r}") from exc
    except OSError as exc:
        raise ScopingError(
            f"git spawn failed while resolving project root for {cwd!r}: {exc}"
        ) from exc

    if r.returncode != 0:
        if "not a git repository" in (r.stderr or "").lower():
            return cwd  # legitimately outside any repo — cwd IS the project root
        raise ScopingError(
            f"git rev-parse failed (rc={r.returncode}) for {cwd!r}: "
            f"{(r.stderr or '').strip()[:200]}"
        )

    common = r.stdout.strip()
    if not common:
        raise ScopingError(f"git returned empty --git-common-dir for {cwd!r}")
    abs_common = os.path.normpath(os.path.join(cwd, common))
    return os.path.dirname(abs_common)


def project_key(root: str) -> tuple[str, str]:
    """Returns (project_id, project_root). project_id is sha256[:16]."""
    pid = hashlib.sha256(root.encode()).hexdigest()[:16]
    return pid, root
