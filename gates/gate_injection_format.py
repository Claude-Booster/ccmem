#!/usr/bin/env python3
"""Gate: injected block has correct delimiter and line format."""
from __future__ import annotations
import re
import sys
from _common import REPO_ROOT, GateResult, base_payload, ensure_seeded_db, injected_text, load_config, run_hook

MARKER_OPEN = "<ccmem-memories>"
MARKER_CLOSE = "</ccmem-memories>"
LINE_RE = re.compile(r"^- \[(\w+) \| ([^]]+)\] .+$")
AGE_RE = re.compile(r"^(<1d|\d+d|\d+w|\d+mo|\?)$")


def main() -> int:
    r = GateResult("injection format")

    db, err = ensure_seeded_db(20)
    if db is None:
        r.fail("test DB seeded", err)
        return r.report()
    r.ok("test DB seeded")

    cfg = load_config()
    payload = base_payload("SessionStart")
    run = run_hook(cfg, "SessionStart", payload, env_extra={"CCMEM_HOME": str(REPO_ROOT / ".ccmem-test")})
    if run.returncode != 0:
        r.fail("hook exits 0", f"rc={run.returncode}")
        return r.report()
    r.ok("hook exits 0")

    text = injected_text(run.stdout)
    if not text:
        r.fail("block emitted", "empty output — no memories injected")
        return r.report()
    r.ok("block emitted", f"{len(text)} chars")

    if MARKER_OPEN not in text:
        r.fail("open marker present", f"expected '{MARKER_OPEN}' in output")
    else:
        r.ok("open marker present")

    if MARKER_CLOSE not in text:
        r.fail("close marker present", f"expected '{MARKER_CLOSE}' in output")
    else:
        r.ok("close marker present")

    body_lines = [ln for ln in text.splitlines() if ln.startswith("- [")]
    if not body_lines:
        r.fail("memory lines present", "no '- [type | age] content' lines found")
        return r.report()
    r.ok("memory lines present", f"{len(body_lines)} lines")

    bad_lines = [ln for ln in body_lines if not LINE_RE.match(ln)]
    if bad_lines:
        r.fail("line format", f"{len(bad_lines)} lines don't match '- [type | age] content': {bad_lines[0]!r}")
    else:
        r.ok("line format")

    bad_ages = []
    for ln in body_lines:
        m = LINE_RE.match(ln)
        if m and not AGE_RE.match(m.group(2)):
            bad_ages.append(m.group(2))
    if bad_ages:
        r.fail("age format", f"invalid age strings: {bad_ages[:3]}")
    else:
        r.ok("age format")

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
