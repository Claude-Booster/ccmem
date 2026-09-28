#!/usr/bin/env python3
"""ccmem acceptance verifier — the fresh-grad runbook as one runnable check.

Runs the whole "does ccmem work?" protocol end to end against a THROWAWAY
sandbox (its own CCMEM_HOME, a fake HOME for the @import files, and disposable
git repos). It never touches your real store or ~/.claude, and cleans up after
itself.

    python scripts/verify.py          # the CLI/loop acceptance checks
    python scripts/verify.py --full   # also run `pytest tests/` and the gate suite
    python scripts/verify.py --keep   # leave the sandbox on disk for inspection

Exit code is 0 only if every check passed. Each check prints one
PASS / FAIL / SKIP line; a failing check never aborts the others.

Why a script and not a pytest module: this is the runbook, made runnable. A
grad runs one command and reads a checklist. It is deliberately black-box —
it drives `python -m ccmem.cli` exactly the way a person would.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Unique, greppable markers so assertions are unambiguous.
PROJ = "PROJ_MEM_ALPHA"
GLOB = "GLOBAL_MEM_BRAVO"
USER = "USER_MEM_CHARLIE"
SUBDIR = "SUBDIR_MEM_DELTA"
SIGIL = "SIGIL_MEM_ECHO"
BLOCKED_WORD = "ZZBLOCKEDZZ"

COUNT_RE = re.compile(r"ccmem: (\d+) of (\d+) memories shown \((\d+) token cap\)")
TOKENS_RE = re.compile(r"~(\d+) / (\d+) tokens")


class Verifier:
    def __init__(self, sandbox: Path):
        self.sandbox = sandbox
        self.home = sandbox / "home"          # fake ~  -> global file lands in home/.claude
        self.store = sandbox / "store"        # CCMEM_HOME (local path, not a cloud sync root)
        self.home.mkdir(parents=True, exist_ok=True)
        self.store.mkdir(parents=True, exist_ok=True)
        self.results: list[tuple[str, str, str]] = []  # (name, status, detail)

    # -- infra ---------------------------------------------------------------

    def env(self) -> dict:
        e = os.environ.copy()
        e["CCMEM_HOME"] = str(self.store)
        e["USERPROFILE"] = str(self.home)   # Windows expanduser("~")
        e["HOME"] = str(self.home)          # POSIX expanduser("~")
        # ccmem must be importable even though cwd is a throwaway repo.
        e["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + e.get("PYTHONPATH", "")
        return e

    def cli(self, *args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ccmem.cli", *args],
            cwd=str(cwd), env=self.env(),
            capture_output=True, text=True,
        )

    def git(self, *args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), env=self.env(),
            capture_output=True, text=True,
        )

    def new_repo(self, name: str) -> Path:
        """A canonical-path git repo. realpath avoids 8.3 / symlink project_id drift."""
        root = Path(os.path.realpath(tempfile.mkdtemp(prefix=name + "-", dir=self.sandbox)))
        self.git("init", cwd=root)
        self.git("config", "user.name", "Developer", cwd=root)
        self.git("config", "user.email", "dev@example.com", cwd=root)
        self.git("config", "commit.gpgsign", "false", cwd=root)
        return root

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        status = "PASS" if ok else "FAIL"
        self.results.append((name, status, detail))
        line = f"  [{status}] {name}"
        if detail:
            line += f"  --  {detail}"
        print(line, flush=True)

    def skip(self, name: str, detail: str) -> None:
        self.results.append((name, "SKIP", detail))
        print(f"  [SKIP] {name}  --  {detail}", flush=True)

    def section(self, title: str, fn) -> None:
        print(f"\n== {title} ==", flush=True)
        try:
            fn()
        except Exception as exc:  # a broken check must not abort the rest
            self.record(title + " (crashed)", False, repr(exc))

    @staticmethod
    def add_id(cp: subprocess.CompletedProcess) -> str:
        m = re.search(r"Added: (\S+)", cp.stdout)
        if not m:
            raise AssertionError(f"no id in add output: {cp.stdout!r} {cp.stderr!r}")
        return m.group(1)

    # -- checks --------------------------------------------------------------

    def check_doctor(self) -> None:
        cp = self.cli("doctor", cwd=self.sandbox)
        self.record("doctor exits cleanly", cp.returncode == 0, cp.stderr.strip()[:120])
        self.record("doctor reports the sandbox store",
                    str(self.store) in cp.stdout,
                    "CCMEM_HOME line should point at the throwaway store")

    def check_crud(self) -> None:
        # global scope so `list` always surfaces it regardless of project id.
        cp = self.cli("add", "--scope", "global", "--content", f"{GLOB} crud", cwd=self.sandbox)
        mid = self.add_id(cp)

        lst = self.cli("list", cwd=self.sandbox)
        self.record("list shows an added memory", GLOB in lst.stdout)

        show = self.cli("show", mid, cwd=self.sandbox)
        self.record("show returns the memory", GLOB in show.stdout and mid in show.stdout)

        pin = self.cli("pin", mid, cwd=self.sandbox)
        self.record("pin succeeds", pin.returncode == 0 and "Pinned" in pin.stdout)
        unpin = self.cli("pin", mid, "--unpin", cwd=self.sandbox)
        self.record("unpin succeeds", unpin.returncode == 0 and "Unpinned" in unpin.stdout)

        self.cli("delete", mid, cwd=self.sandbox)
        after_del = self.cli("show", mid, cwd=self.sandbox)
        self.record("delete marks status=deleted", "Status: deleted" in after_del.stdout)
        self.cli("restore", mid, cwd=self.sandbox)
        after_res = self.cli("show", mid, cwd=self.sandbox)
        self.record("restore marks status=active", "Status: active" in after_res.stdout)

    def check_loop_and_scope(self) -> None:
        """add (project/global/user) -> generate -> assert scope isolation."""
        proj = self.new_repo("proj")
        self.proj = proj  # reused by determinism + gitignore + sigil checks

        self.cli("add", "--scope", "project", "--content", f"{PROJ} decision", cwd=proj)
        self.cli("add", "--scope", "global", "--content", f"{GLOB} fact", cwd=proj)
        self.cli("add", "--scope", "user", "--content", f"{USER} pref", cwd=proj)

        gen = self.cli("generate", cwd=proj)
        self.record("generate exits cleanly", gen.returncode == 0, gen.stderr.strip()[:120])

        proj_file = proj / ".ccmem" / "memories.md"
        glob_file = self.home / ".claude" / "ccmem-memories.md"
        self.record("project file written", proj_file.exists())
        self.record("global file written", glob_file.exists())

        ptext = proj_file.read_text(encoding="utf-8") if proj_file.exists() else ""
        gtext = glob_file.read_text(encoding="utf-8") if glob_file.exists() else ""

        self.record("project memory in project file", PROJ in ptext)
        self.record("global+user memories in global file", GLOB in gtext and USER in gtext)
        # The isolation that matters: scopes do not bleed across files.
        self.record("project scope absent from global file", PROJ not in gtext)
        self.record("global/user scopes absent from project file",
                    GLOB not in ptext and USER not in ptext)

    def check_subdir_scoping(self) -> None:
        """An add from a subdirectory must scope to the repo root, not the subdir."""
        proj = getattr(self, "proj", None) or self.new_repo("proj")
        sub = proj / "a" / "deep" / "sub"
        sub.mkdir(parents=True, exist_ok=True)
        self.cli("add", "--scope", "project", "--content", f"{SUBDIR} note", cwd=sub)
        self.cli("generate", "--project-only", cwd=proj)
        ptext = (proj / ".ccmem" / "memories.md").read_text(encoding="utf-8")
        self.record("subdir add is repo-scoped (visible from repo root)", SUBDIR in ptext,
                    "if FAIL, resolve_project_root did not canonicalize to the git root")

    def check_determinism(self) -> None:
        proj = getattr(self, "proj", None) or self.new_repo("proj")
        self.cli("generate", cwd=proj)
        b1p = (proj / ".ccmem" / "memories.md").read_bytes()
        b1g = (self.home / ".claude" / "ccmem-memories.md").read_bytes()
        self.cli("generate", cwd=proj)
        b2p = (proj / ".ccmem" / "memories.md").read_bytes()
        b2g = (self.home / ".claude" / "ccmem-memories.md").read_bytes()
        self.record("project generate is byte-identical across runs", b1p == b2p)
        self.record("global generate is byte-identical across runs", b1g == b2g)

    def check_gitignore(self) -> None:
        proj = getattr(self, "proj", None) or self.new_repo("proj")
        self.cli("generate", "--project-only", cwd=proj)
        gi = proj / ".ccmem" / ".gitignore"
        self.record(".ccmem/.gitignore is '*'",
                    gi.exists() and gi.read_text(encoding="utf-8").strip() == "*")
        ci = self.git("check-ignore", "-q", ".ccmem/memories.md", cwd=proj)
        self.record("git actually ignores .ccmem/memories.md", ci.returncode == 0,
                    "git check-ignore must confirm it, not just the written file")

    def check_cap_and_truncation(self) -> None:
        """Overflow the whole-file cap; the count line must signal truncation."""
        cap = Path(os.path.realpath(tempfile.mkdtemp(prefix="capdir-", dir=self.sandbox)))
        # 6 x ~600 chars (~150 tok each) ~= 900 tok > 800 project cap -> must truncate.
        for i in range(6):
            self.cli("add", "--scope", "project",
                     "--content", f"CAPFILL-{i} " + ("x" * 590), cwd=cap)
        gen = self.cli("generate", "--project-only", cwd=cap)
        text = (cap / ".ccmem" / "memories.md").read_text(encoding="utf-8")
        m = COUNT_RE.search(text)
        if not m:
            self.record("count line present", False, f"no count line in {text[:80]!r}")
            return
        shown, total, capval = int(m.group(1)), int(m.group(2)), int(m.group(3))
        self.record("all memories counted (M=6)", total == 6, f"total={total}")
        self.record("cap truncates (shown < total)", shown < total, f"{shown} of {total}")
        tm = TOKENS_RE.search(gen.stdout)
        est = int(tm.group(1)) if tm else -1
        self.record("generated file within the token cap", 0 <= est <= capval,
                    f"~{est} / {capval} tokens")

    def check_sigil_capture(self) -> None:
        """A !mem: line in a transcript is swept into memory by `capture`."""
        proj = getattr(self, "proj", None) or self.new_repo("proj")
        tpath = self.sandbox / "session.jsonl"
        # cwd on the user record must match the repo so the sigil scopes there.
        # timestamp omitted -> treated as post-install, always captured.
        lines = [
            {"type": "user", "cwd": str(proj),
             "message": {"content": [{"type": "text", "text": f"!mem: {SIGIL} always sweep"}]}},
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "noted"}]}},
        ]
        tpath.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        cp = self.cli("capture", str(tpath), "--session-id", "verify-sigil", cwd=proj)
        # anchored: "sigil_memories=1" alone would also match =10, =11, ...
        self.record("capture reports one sigil memory",
                    re.search(r"sigil_memories=1\b", cp.stdout) is not None,
                    cp.stdout.strip()[:80])
        self.cli("generate", "--project-only", cwd=proj)
        ptext = (proj / ".ccmem" / "memories.md").read_text(encoding="utf-8")
        self.record("swept sigil reaches the generated project file", SIGIL in ptext)

    def check_guard_hooks(self) -> None:
        if not shutil.which("git"):
            self.skip("guard hooks", "git not on PATH")
            return
        repo = self.new_repo("hookrepo")
        hooks = repo / ".githooks"
        hooks.mkdir()
        for name in ("pre-commit", "commit-msg", "pre-push", "tag"):
            src = REPO_ROOT / ".githooks" / name
            if src.exists():
                shutil.copy2(src, hooks / name)
        # our own throwaway pattern — never the real .blocked identifiers
        (hooks / ".blocked").write_text(BLOCKED_WORD + "\n", encoding="utf-8")
        self.git("config", "core.hooksPath", ".githooks", cwd=repo)

        # block case: staged content carries the blocked word
        (repo / "secret.txt").write_text(f"contains {BLOCKED_WORD}\n", encoding="utf-8")
        self.git("add", "secret.txt", cwd=repo)
        blocked = self.git("commit", "-m", "should be blocked", cwd=repo)
        # Assert it failed FOR THE RIGHT REASON, not just any non-zero exit.
        self.record("pre-commit blocks a restricted identifier",
                    blocked.returncode != 0 and "BLOCKED" in (blocked.stderr + blocked.stdout),
                    "commit must be refused by the guard hook")

        # allow case: clean content commits fine
        (repo / "secret.txt").write_text("clean content\n", encoding="utf-8")
        self.git("add", "secret.txt", cwd=repo)
        clean = self.git("commit", "-m", "clean commit", cwd=repo)
        self.record("clean commit is allowed", clean.returncode == 0,
                    (clean.stderr or clean.stdout).strip()[:120])

    def check_design_verdict(self) -> None:
        design = (REPO_ROOT / "docs" / "DESIGN.md")
        text = design.read_text(encoding="utf-8") if design.exists() else ""
        self.record("DESIGN records the 'heuristic scoring is dead' verdict",
                    "heuristic" in text.lower() and "dead" in text.lower())
        self.record("DESIGN records the collapsed sigil-sweep direction",
                    "sigil" in text.lower() and "pending trial" in text.lower())

    # -- --full extras -------------------------------------------------------

    def check_pytest(self) -> None:
        cp = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q"],
            cwd=str(REPO_ROOT), env=self.env(), capture_output=True, text=True,
        )
        tail = cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else cp.stderr.strip()[:120]
        self.record("pytest suite passes", cp.returncode == 0, tail)

    def check_gates(self) -> None:
        cp = subprocess.run(
            [sys.executable, "gates/run_gates.py", "--phase", "1"],
            cwd=str(REPO_ROOT), env=self.env(), capture_output=True, text=True,
        )
        # gate_phase1_notes is red by design until docs/PHASE1-NOTES.md is written.
        allowed = {"gate_phase1_notes"}
        if cp.returncode == 0:
            self.record("gate suite passes (phase 1)", True, "all green")
            return
        m = re.search(r"gate\(s\) failing: (.+)", cp.stdout)
        if not m:
            # Non-zero exit with no parseable failing-list: the runner crashed, hit
            # the argparse/"no gates" path, or changed its summary wording. Never
            # tolerate what we could not read — a false green here is the worst
            # outcome (the whole point is to not let a red suite look green).
            tail = ((cp.stdout + cp.stderr).strip().splitlines() or [""])[-1]
            self.record("gate suite passes (phase 1)", False,
                        f"could not parse run_gates output (rc={cp.returncode}): {tail[:80]}")
            return
        failing = {g.strip() for g in m.group(1).split(",")}
        ok = bool(failing) and failing <= allowed
        detail = f"failing: {', '.join(sorted(failing))}"
        if ok:
            detail += " (expected)"
        self.record("gate suite passes (phase 1)", ok, detail)

    # -- driver --------------------------------------------------------------

    def run(self, full: bool) -> int:
        self.section("Health check (doctor)", self.check_doctor)
        self.section("CRUD round-trip", self.check_crud)
        self.section("Core loop + scope isolation", self.check_loop_and_scope)
        self.section("Subdirectory scoping", self.check_subdir_scoping)
        self.section("Generate determinism", self.check_determinism)
        self.section("gitignore protection", self.check_gitignore)
        self.section("Token cap + truncation", self.check_cap_and_truncation)
        self.section("Sigil capture", self.check_sigil_capture)
        self.section("Guard hooks", self.check_guard_hooks)
        self.section("DESIGN verdict recorded", self.check_design_verdict)
        if full:
            self.section("Full: pytest", self.check_pytest)
            self.section("Full: gates", self.check_gates)

        passed = sum(1 for _, s, _ in self.results if s == "PASS")
        failed = sum(1 for _, s, _ in self.results if s == "FAIL")
        skipped = sum(1 for _, s, _ in self.results if s == "SKIP")
        print("\n" + "=" * 60)
        print(f"SUMMARY: {passed} passed, {failed} failed, {skipped} skipped")
        if failed:
            print("Failed checks:")
            for name, status, detail in self.results:
                if status == "FAIL":
                    print(f"  - {name}  ({detail})" if detail else f"  - {name}")
        print("=" * 60)
        return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ccmem acceptance verifier")
    ap.add_argument("--full", action="store_true",
                    help="also run the pytest suite and the phase-1 gates")
    ap.add_argument("--keep", action="store_true",
                    help="leave the sandbox on disk instead of deleting it")
    args = ap.parse_args()

    sandbox = Path(tempfile.mkdtemp(prefix="ccmem-verify-"))
    print(f"Sandbox: {sandbox}")
    print(f"Repo:    {REPO_ROOT}")
    try:
        return Verifier(sandbox).run(full=args.full)
    finally:
        if args.keep:
            print(f"\nSandbox kept at: {sandbox}")
        else:
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
