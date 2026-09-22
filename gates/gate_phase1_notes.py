#!/usr/bin/env python3
"""Gate: Phase 1 usage notes (absorbs skeleton's Phase 0 intent).

Phase 1 is explicit-capture-plus-review. The skeleton's Phase 0 plan was to
hand-maintain a markdown memory bank for a week to find out what's actually
worth remembering before retrieval machinery could hide the question. Phase 1
collapses that into itself: !mem: into SQLite is the same exercise with a
better store.

The exit criterion is docs/PHASE1-NOTES.md — a running log written during
real Phase 1 use. It should record:
  - What you actually wrote down (what felt memorable enough to capture)
  - What you wished you'd written down (things you re-explained that !mem: missed)
  - What went stale fastest (memories that were wrong a week later)

This file is the empirical input to the Phase 3 classifier: if the LLM extractor
is going to learn what to capture automatically, these notes are the training
signal. The gate cannot judge quality, but it can enforce that the notes exist and
have substance before Phase 2 begins.

Run: python gates/gate_phase1_notes.py
"""

from __future__ import annotations

import sys

from _common import REPO_ROOT, GateResult, estimate_tokens

NOTES_PATH = "docs/PHASE1-NOTES.md"
REQUIRED_HEADINGS = ["wrote down", "wished", "stale"]  # substring match, case-insensitive
MIN_WORDS = 200
MAX_TOKENS = 500  # notes are for your eyes, not the context window


def main() -> int:
    r = GateResult("phase 1 usage notes")

    notes = REPO_ROOT / NOTES_PATH
    if not notes.exists():
        r.fail(
            f"{NOTES_PATH} exists",
            "create this file during Phase 1 use — it is the input to Phase 3",
        )
        return r.report()
    r.ok(f"{NOTES_PATH} exists")

    text = notes.read_text(encoding="utf-8", errors="replace")
    words = len(text.split())
    if words < MIN_WORDS:
        r.fail(
            "notes have substance",
            f"{words} words — want >= {MIN_WORDS}; sparse notes mean the phase wasn't used",
        )
    else:
        r.ok("notes have substance", f"{words} words")

    tokens = estimate_tokens(text)
    if tokens > MAX_TOKENS:
        r.fail(
            "notes are concise",
            f"~{tokens} tokens — want <= {MAX_TOKENS}; "
            "notes are for you, not the context window; trim or summarise",
        )
    else:
        r.ok("notes are concise", f"~{tokens} tokens")

    text_lower = text.lower()
    for heading in REQUIRED_HEADINGS:
        if heading not in text_lower:
            r.fail(
                f"notes cover '{heading}'",
                f"no line containing '{heading}' found — "
                "notes must address what you wrote, what you missed, and what went stale",
            )
        else:
            r.ok(f"notes cover '{heading}'")

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
