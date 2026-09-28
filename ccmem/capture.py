from __future__ import annotations
import hashlib
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from ccmem.transcript import iter_turn_pairs, normalize_transcript_path

_PATTERNS: list[tuple[float, re.Pattern]] = [
    (3, re.compile(r"\b(we decided|we'?re going with|the approach is|going with)\b", re.I)),
    (3, re.compile(r"\b(from now on|always|never|going forward)\b", re.I)),
    (2, re.compile(r"\b(you were wrong|that'?s incorrect|actually)\b", re.I)),
    (2, re.compile(r"\buse .+ instead of\b|\bswitch to\b|\breplace with\b", re.I)),
    (2, re.compile(r"```[^\n]*\n[-+]", re.M)),  # diff block heuristic
    (1, re.compile(r"\b(the reason|because|in order to)\b", re.I)),
]

_SIGIL_RE = re.compile(r"^!mem(?:\[(?P<scope>[a-z]+)\])?:\s*(?P<text>.+)", re.DOTALL)

MAX_CANDIDATE_CHARS = int(os.environ.get("CCMEM_MAX_CANDIDATE_CHARS", "2000"))


def content_hash(user_turn: str, assistant_turn: str) -> str:
    h = hashlib.sha256()
    h.update(user_turn.encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(assistant_turn.encode("utf-8", "replace"))
    return h.hexdigest()


@dataclass
class CaptureResult:
    candidates: int = 0
    sigil_memories: int = 0
    refusals: int = 0
    promptid_drift: bool = False


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
):
    h = content_hash(user_turn, assistant_turn)          # FULL text
    before = con.total_changes
    con.execute(
        "INSERT OR IGNORE INTO candidates "
        "(id, session_id, prompt_id, user_turn, assistant_turn, "
        "classifier_score, is_pre_compact, created_at, status, content_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),   # ccmem: cache-safe — id is stored, not injected
            session_id,
            prompt_id,
            user_turn[:MAX_CANDIDATE_CHARS], assistant_turn[:MAX_CANDIDATE_CHARS],
            score,
            1 if is_pre_compact else 0,
            datetime.now(timezone.utc).isoformat(),  # ccmem: cache-safe
            "pending", h,
        ),
    )
    con.commit()
    return con.total_changes - before   # >0 inserted, 0 deduped


def _handle_sigil(con, pair, text, scope, session_id, norm_path):
    """Write a durable memory, record a refusal, or dedup.
    Returns (wrote: bool, refused: bool): (True,False) inserted, (False,True) refused,
    (False,False) deduped no-op."""
    import hashlib
    from ccmem.redact import redact
    from ccmem.supersession import maybe_supersede
    from ccmem.scoping import ScopingError, project_key, resolve_project_root
    redacted = redact(text)
    if redacted != text:
        con.execute(
            "INSERT INTO sigil_refusals (id, transcript_path, session_id, created_at, excerpt) "
            "VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), norm_path, session_id,
             datetime.now(timezone.utc).isoformat(), redacted[:200]),
        )
        con.commit()
        return (False, True)
    h = hashlib.sha256(redacted.encode("utf-8", "replace")).hexdigest()
    try:
        root = resolve_project_root(pair.cwd or os.getcwd())
    except ScopingError:
        # A `!mem:` sigil is an EXPLICIT "remember this" — never drop it over a
        # scoping hiccup (e.g. the session's cwd no longer exists on disk). Fall back
        # to the raw cwd so the memory is preserved; an imperfect scope beats losing
        # an instruction the user deliberately gave. (resolve_project_root stays
        # strict for add/generate/review, where the user is present to see the error.)
        root = pair.cwd or os.getcwd()
    pid, _ = project_key(root)
    mem_id = str(uuid.uuid4())
    before = con.total_changes
    con.execute(
        "INSERT OR IGNORE INTO memories "
        "(id, type, content, scope, project_id, project_root, created_at, status, content_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (mem_id, "preference", redacted, scope or "project", pid, root,
         datetime.now(timezone.utc).isoformat(), "active", h),
    )
    con.commit()
    if con.total_changes == before:
        return (False, False)  # deduped by content_hash — identical sigil already stored
    maybe_supersede(con, mem_id, None, pid, scope or "project")
    return (True, False)


def _read_hwm(con, norm_path):
    row = con.execute(
        "SELECT last_prompt_id, last_ordinal FROM transcript_progress WHERE transcript_path=?",
        (norm_path,),
    ).fetchone()
    return (row[0], row[1]) if row else (None, -1)


def _write_hwm(con, norm_path, session_id, last_prompt_id, last_ordinal):
    con.execute(
        "INSERT INTO transcript_progress "
        "(transcript_path, last_prompt_id, last_ordinal, session_id, updated_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(transcript_path) DO UPDATE SET "
        "last_prompt_id=excluded.last_prompt_id, last_ordinal=excluded.last_ordinal, "
        "session_id=excluded.session_id, updated_at=excluded.updated_at",
        (norm_path, last_prompt_id, last_ordinal, session_id,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    con.commit()


def _initialized_at(con) -> str:
    row = con.execute(
        "SELECT value FROM schema_meta WHERE key='initialized_at'"
    ).fetchone()
    return row[0] if row else "1970-01-01T00:00:00Z"


def capture_transcript(con, transcript_path, session_id, is_pre_compact=False):
    norm = normalize_transcript_path(transcript_path)
    last_pid, last_ord = _read_hwm(con, norm)
    init_at = _initialized_at(con)
    result = CaptureResult()
    threshold = float(os.environ.get("CCMEM_THRESHOLD", "4"))
    seen_pid = seen_user = 0
    max_pid, max_ord = last_pid, last_ord
    for pair in iter_turn_pairs(transcript_path):
        seen_user += 1
        if pair.prompt_id is not None:
            seen_pid += 1
        # Position by ordinal: it is always present and stable for an append-only
        # transcript (the Nth user record is always the Nth). prompt_id is stored as
        # the HWM pointer and used only for drift detection — this is why mixed
        # promptId presence (change #7) cannot cause a skip or re-process.
        if pair.ordinal <= last_ord:
            continue
        # Per-turn install bound (finding #4): skip turns from before ccmem existed,
        # even in a resumed pre-install session whose file mtime is now current.
        # A missing/unparseable timestamp is treated as post-install (captured).
        if pair.timestamp is not None and pair.timestamp < init_at:
            max_ord = max(max_ord, pair.ordinal)   # advance HWM past it; never capture
            if pair.prompt_id is not None:
                max_pid = pair.prompt_id
            continue
        sigil_text, sigil_scope, _ = extract_sigil(pair.user_turn)
        if sigil_text is not None:
            wrote, refused = _handle_sigil(con, pair, sigil_text, sigil_scope, session_id, norm)
            if wrote:
                result.sigil_memories += 1
            elif refused:
                result.refusals += 1
            # deduped no-op: neither counter moves
            max_ord = max(max_ord, pair.ordinal)
            if pair.prompt_id is not None:
                max_pid = pair.prompt_id
            continue
        score = score_turn(pair.user_turn, pair.assistant_turn)
        if score >= threshold:
            if enqueue_candidate(con, session_id, pair.prompt_id,
                                 pair.user_turn, pair.assistant_turn, score,
                                 is_pre_compact):
                result.candidates += 1
        max_ord = max(max_ord, pair.ordinal)
        if pair.prompt_id is not None:
            max_pid = pair.prompt_id
    if seen_user > 0 and seen_pid == 0:
        result.promptid_drift = True
    if max_ord != last_ord or max_pid != last_pid:
        _write_hwm(con, norm, session_id, max_pid, max_ord)
    return result
