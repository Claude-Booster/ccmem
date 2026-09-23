import subprocess
import sys
from pathlib import Path
import os

REPO = Path(__file__).parent.parent


def run_cli(*args, db_dir=None, input_text=None):
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
