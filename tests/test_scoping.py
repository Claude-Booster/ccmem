import subprocess
from pathlib import Path
from unittest.mock import patch
import pytest
from ccmem.scoping import resolve_project_root, project_key, ScopingError

REPO = Path(__file__).parent.parent


def test_resolve_returns_repo_root_for_subdir():
    root = resolve_project_root(str(REPO / "gates"))
    assert root == str(REPO)


def test_resolve_returns_cwd_when_not_git(tmp_path):
    # A real directory that is genuinely not inside a git repo -> cwd is the root.
    root = resolve_project_root(str(tmp_path))
    assert root == str(tmp_path)


def test_resolve_raises_when_git_not_found():
    # git missing is NOT "not a repo" — it must be loud, not a silent cwd fallback.
    with patch("ccmem.scoping.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(ScopingError):
            resolve_project_root("/some/path")


def test_resolve_raises_on_timeout():
    with patch("ccmem.scoping.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
        with pytest.raises(ScopingError):
            resolve_project_root("/some/path")


def test_resolve_raises_on_spawn_oserror():
    # The WinError-6 class of failure that silently mis-scoped memories.
    with patch("ccmem.scoping.subprocess.run", side_effect=OSError(6, "handle invalid")):
        with pytest.raises(ScopingError):
            resolve_project_root("/some/path")


def test_resolve_raises_on_unexpected_git_error():
    fake = subprocess.CompletedProcess([], 128, stdout="", stderr="fatal: bad object HEAD")
    with patch("ccmem.scoping.subprocess.run", return_value=fake):
        with pytest.raises(ScopingError):
            resolve_project_root("/some/path")


def test_resolve_returns_cwd_on_genuine_not_a_repo():
    fake = subprocess.CompletedProcess(
        [], 128, stdout="",
        stderr="fatal: not a git repository (or any of the parent directories): .git")
    with patch("ccmem.scoping.subprocess.run", return_value=fake):
        assert resolve_project_root("/some/path") == "/some/path"


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
