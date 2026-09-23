import subprocess
from pathlib import Path
from unittest.mock import patch
from ccmem.scoping import resolve_project_root, project_key

REPO = Path(__file__).parent.parent


def test_resolve_returns_repo_root_for_subdir():
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
