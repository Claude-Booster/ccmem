from __future__ import annotations
import re
import sqlite3
import uuid
from datetime import datetime, timezone

_PATTERNS: list[tuple[float, re.Pattern]] = [
    (3, re.compile(r"\b(we decided|we'?re going with|the approach is|going with)\b", re.I)),
    (3, re.compile(r"\b(from now on|always|never|going forward)\b", re.I)),
    (2, re.compile(r"\b(you were wrong|that'?s incorrect|actually)\b", re.I)),
    (2, re.compile(r"\buse .+ instead of\b|\bswitch to\b|\breplace with\b", re.I)),
    (2, re.compile(r"```[^\n]*\n[-+]", re.M)),  # diff block heuristic
    (1, re.compile(r"\b(the reason|because|in order to)\b", re.I)),
]

_SIGIL_RE = re.compile(r"^!mem(?:\[(?P<scope>[a-z]+)\])?:\s*(?P<text>.+)", re.DOTALL)


def score_turn(user_text: str, assistant_text: str) -> float:
    combined = user_text + "\n" + assistant_text
    total = 0.0
    for points, pattern in _PATTERNS:
        if pattern.search(combined):
            total += points
    return total


def extract_sigil(prompt: str) -> tuple[str | None, str | None, str]:
    """Returns (annotation_text, scope, cleaned_prompt). text is None if no sigil."""
    m = _SIGIL_RE.match(prompt.strip())
    if not m:
        return None, None, prompt
    text = m.group("text").strip()
    scope = m.group("scope") or "project"
    cleaned = re.sub(r"^!mem(?:\[[a-z]+\])?:[^\n]*\n?", "", prompt.strip(), count=1)
    return text, scope, cleaned.strip()


def enqueue_candidate(
    con: sqlite3.Connection,
    session_id: str,
    prompt_id: str | None,
    user_turn: str,
    assistant_turn: str,
    score: float,
    is_pre_compact: bool = False,
) -> None:
    con.execute(
        "INSERT OR IGNORE INTO candidates "
        "(id, session_id, prompt_id, user_turn, assistant_turn, "
        "classifier_score, is_pre_compact, created_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),   # ccmem: cache-safe — id is stored, not injected
            session_id,
            prompt_id,
            user_turn,
            assistant_turn,
            score,
            1 if is_pre_compact else 0,
            datetime.now(timezone.utc).isoformat(),  # ccmem: cache-safe
            "pending",
        ),
    )
    con.commit()
