from __future__ import annotations
import hashlib
import os
from pathlib import Path

_DIR = "disabled"


def session_id_from_transcript(transcript_path: str) -> str:
    """FACTS §4: the transcript filename stem is the session-uuid."""
    return os.path.splitext(os.path.basename(transcript_path))[0]


def _marker(home: str, session_id: str) -> Path:
    key = hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()
    return Path(home) / _DIR / key


def mark_disabled(home: str, session_id: str) -> bool:
    """Tombstone a session so no future sweep captures its transcript. Not capture.
    Returns True on success; False if the marker could not be written (finding #2 —
    the caller surfaces a systemMessage in that case).
    Writes session_id as content so `ccmem doctor` can display which sessions are tombstoned."""
    try:
        m = _marker(home, session_id)
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text(session_id, encoding="utf-8")
        return m.exists()
    except Exception:
        return False


def list_disabled(home: str) -> list[str]:
    """Return session_ids of all tombstoned sessions. Empty list if none or on error."""
    try:
        d = Path(home) / _DIR
        if not d.exists():
            return []
        result = []
        for f in sorted(d.iterdir()):
            try:
                sid = f.read_text(encoding="utf-8").strip()
                result.append(sid if sid else f.name)
            except Exception:
                result.append(f.name)  # old-format empty marker: show hash
        return result
    except Exception:
        return []


def is_disabled(home: str, session_id: str) -> bool:
    try:
        return _marker(home, session_id).exists()
    except Exception:
        return False
