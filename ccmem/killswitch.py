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
    the caller surfaces a systemMessage in that case)."""
    try:
        m = _marker(home, session_id)
        m.parent.mkdir(parents=True, exist_ok=True)
        m.touch(exist_ok=True)
        return m.exists()
    except Exception:
        return False


def is_disabled(home: str, session_id: str) -> bool:
    try:
        return _marker(home, session_id).exists()
    except Exception:
        return False
