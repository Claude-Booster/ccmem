#!/usr/bin/env python3
"""Gate: hook contract (CLAUDE.md R1, R6).

A memory tool that can wedge the CLI is worse than no memory tool. Every hook
must survive garbage input, must exit 0, and must only inject context from the
two events that Claude Code actually injects from.

Run: python gates/gate_hook_contract.py
"""

from __future__ import annotations

import json
import sys

from _common import (
    INJECTING_EVENTS,
    GateResult,
    base_payload,
    hook_path,
    injected_text,
    load_config,
    parse_hook_output,
    run_hook,
)

# Inputs a hook must survive. Claude Code should never send most of these; the
# point is that a bug upstream must not become a broken session for the user.
HOSTILE_INPUTS = {
    "empty stdin": b"",
    "not json": b"this is not json at all",
    "truncated json": b'{"session_id": "x", "cwd"',
    "null json": b"null",
    "json array": b"[]",
    "missing required keys": json.dumps({"hook_event_name": "SessionStart"}).encode(),
    "wrong types": json.dumps({"cwd": 42, "session_id": None}).encode(),
}


def main() -> int:
    cfg = load_config()
    r = GateResult("hook contract")

    for event, rel in cfg["hooks"].items():
        script = hook_path(cfg, event)
        if not script.exists():
            r.fail(f"{event}: exists", f"missing {rel} -- not implemented yet")
            continue
        r.ok(f"{event}: exists", rel)

        budget = cfg["budget_ms"].get(event, 2000)

        # --- happy path -------------------------------------------------
        run = run_hook(cfg, event, base_payload(event))
        if run.timed_out:
            r.fail(f"{event}: valid payload", "hook hung")
            continue
        if run.returncode != 0:
            r.fail(
                f"{event}: valid payload exits 0",
                f"rc={run.returncode} stderr={run.stderr.strip()[:200]}",
            )
        else:
            r.ok(f"{event}: valid payload exits 0", f"{run.elapsed_ms:.0f}ms")

        if run.elapsed_ms > budget:
            r.fail(
                f"{event}: within budget",
                f"{run.elapsed_ms:.0f}ms > {budget}ms -- move work off the hook path",
            )
        else:
            r.ok(f"{event}: within budget", f"{run.elapsed_ms:.0f}ms <= {budget}ms")

        # --- output shape -----------------------------------------------
        obj = parse_hook_output(run.stdout)
        if obj is not None:
            hso = obj.get("hookSpecificOutput")
            if hso is not None:
                declared = hso.get("hookEventName")
                if declared != event:
                    r.fail(
                        f"{event}: hookEventName matches",
                        f"declared {declared!r}, invoked as {event!r}",
                    )
                else:
                    r.ok(f"{event}: hookEventName matches")

            if obj.get("decision") == "block":
                r.fail(f"{event}: does not block", "hook returned decision=block")
            else:
                r.ok(f"{event}: does not block")

            if obj.get("continue") is False:
                r.fail(f"{event}: does not halt session", "returned continue=false")
            else:
                r.ok(f"{event}: does not halt session")

        ctx = injected_text(run.stdout)
        if event in INJECTING_EVENTS:
            if len(ctx) > cfg["max_injected_chars"]:
                r.fail(
                    f"{event}: injected size",
                    f"{len(ctx)} chars > {cfg['max_injected_chars']} cap",
                )
            else:
                r.ok(f"{event}: injected size", f"{len(ctx)} chars")
        else:
            # Non-injecting events: stdout goes to the debug log, so emitting
            # context there is dead weight at best and a cache hazard at worst.
            if ctx.strip():
                r.fail(
                    f"{event}: emits no context",
                    "non-injecting event produced output that will never reach the model",
                )
            else:
                r.ok(f"{event}: emits no context")

        # --- hostile inputs ---------------------------------------------
        survived = True
        for label, blob in HOSTILE_INPUTS.items():
            bad = run_hook(cfg, event, blob)
            if bad.timed_out or bad.returncode != 0:
                survived = False
                r.fail(
                    f"{event}: survives {label}",
                    f"rc={bad.returncode} timed_out={bad.timed_out}",
                )
        if survived:
            r.ok(f"{event}: survives hostile input", f"{len(HOSTILE_INPUTS)} cases")

        # --- exit 2 is forbidden on the prompt path ---------------------
        if event == "UserPromptSubmit":
            # Exit 2 here blocks and erases the user's prompt. Never acceptable.
            oversized = base_payload(event) | {"prompt": "x" * 1_000_000}
            big = run_hook(cfg, event, oversized)
            if big.returncode == 2 or big.timed_out:
                r.fail(
                    "UserPromptSubmit: never exits 2",
                    f"rc={big.returncode} on 1MB prompt -- would erase the prompt",
                )
            else:
                r.ok("UserPromptSubmit: never exits 2", "1MB prompt handled")

        # --- kill switch -------------------------------------------------
        # Threshold: interpreter_floor_ms + 150ms headroom. The floor is the cost
        # of spawning python at all on this machine; anything beyond that budget is
        # real work done before the CCMEM_DISABLED check — which is the bug.
        floor = cfg.get("interpreter_floor_ms", 300)
        kill_threshold = floor + 200  # 200ms: floor is python -c pass; hooks add ~50ms of stdlib imports before the check
        off = run_hook(cfg, event, base_payload(event), env_extra={"CCMEM_DISABLED": "1"})
        if off.returncode != 0:
            r.fail(f"{event}: kill switch exits 0", f"rc={off.returncode}")
        elif injected_text(off.stdout).strip():
            r.fail(f"{event}: kill switch injects nothing", "produced context anyway")
        elif off.elapsed_ms > kill_threshold:
            r.fail(
                f"{event}: kill switch is fast",
                f"{off.elapsed_ms:.0f}ms > floor({floor})+200={kill_threshold}ms "
                "-- should short-circuit before any I/O",
            )
        else:
            r.ok(f"{event}: kill switch is fast", f"{off.elapsed_ms:.0f}ms <= {kill_threshold}ms")

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
