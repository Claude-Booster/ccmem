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
    except (OSError, subprocess.TimeoutExpired):
        return cwd


def project_key(root: str) -> tuple[str, str]:
    """Returns (project_id, project_root). project_id is sha256[:16]."""
    pid = hashlib.sha256(root.encode()).hexdigest()[:16]
    return pid, root
