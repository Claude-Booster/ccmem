"""Transcript parsing for ccmem.

Reads Claude Code transcript JSONL files and yields user/assistant turn-pairs.
Robust to partial/truncated lines, CRLF line endings, and non-UTF-8 bytes
(R1 spirit: never abort a file on malformed input).
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass
from typing import Iterator


def normalize_transcript_path(path: str) -> str:
    """Return a canonical, case-folded absolute path for deduplication."""
    return os.path.normcase(os.path.realpath(path))


@dataclass
class TurnPair:
    prompt_id: str | None
    ordinal: int
    user_turn: str
    assistant_turn: str
    cwd: str | None
    timestamp: str | None


def _text_blocks(rec: dict) -> str:
    parts = []
    for block in rec.get("message", {}).get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def iter_turn_pairs(transcript_path: str) -> Iterator[TurnPair]:
    """Yield (user, following-assistant-text) pairs in order.

    Robust to a partial/truncated final line and to non-JSON lines: a line
    that does not parse to a dict is skipped, never aborts the file. ``ordinal``
    is the 0-based index of the ``user`` record; ``timestamp`` is the user
    record's ``timestamp`` field (used for the per-turn install bound).
    """
    try:
        fh = open(transcript_path, encoding="utf-8", errors="replace")
    except OSError:
        return
    ordinal = -1
    pending = None  # (prompt_id, ordinal, user_text, cwd, timestamp)
    asst: list[str] = []
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue  # partial or malformed line — skip, keep going
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")
            if rtype == "user":
                if pending is not None:
                    yield TurnPair(pending[0], pending[1], pending[2],
                                   "".join(asst), pending[3], pending[4])
                ordinal += 1
                pending = (rec.get("promptId"), ordinal, _text_blocks(rec),
                           rec.get("cwd"), rec.get("timestamp"))
                asst = []
            elif rtype == "assistant" and pending is not None:
                asst.append(_text_blocks(rec))
        if pending is not None:
            yield TurnPair(pending[0], pending[1], pending[2], "".join(asst),
                           pending[3], pending[4])
