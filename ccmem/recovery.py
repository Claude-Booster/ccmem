from __future__ import annotations
import glob
import os
import time
from ccmem.capture import CaptureResult, capture_transcript, normalize_transcript_path
from ccmem.killswitch import is_disabled, session_id_from_transcript


def _updated_at(con, norm_path):
    row = con.execute(
        "SELECT updated_at FROM transcript_progress WHERE transcript_path=?", (norm_path,)
    ).fetchone()
    return row[0] if row else None


def recover_project(con, home, project_dir, initialized_at, max_bytes, max_ms) -> CaptureResult:
    """Sweep transcripts in project_dir that have unprocessed turns, bounded by
    cumulative bytes and a wall-clock deadline. Partial sweeps are safe: the HWM
    and content_hash guarantee no loss and no duplication across calls."""
    agg = CaptureResult()
    deadline = time.monotonic() + (max_ms / 1000.0)
    bytes_read = 0
    try:
        files = glob.glob(os.path.join(project_dir, "*.jsonl"))
    except Exception:
        return agg
    statted = []
    for f in files:
        try:
            statted.append((f, os.stat(f)))
        except OSError:
            continue
    statted.sort(key=lambda p: p[1].st_mtime, reverse=True)  # most-recent first
    for f, st in statted:
        if time.monotonic() >= deadline or bytes_read >= max_bytes:
            break
        mtime_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))
        if mtime_iso <= initialized_at:
            continue  # stat-only prefilter; the real bound is per-turn in capture_transcript
        sid = session_id_from_transcript(f)
        if is_disabled(home, sid):
            continue  # kill switch (session_id-keyed, finding #1)
        norm = normalize_transcript_path(f)
        ua = _updated_at(con, norm)
        if ua is not None and mtime_iso <= ua:
            continue  # already current (stat-only, no read)
        r = capture_transcript(con, f, session_id=sid)
        agg.candidates += r.candidates
        agg.sigil_memories += r.sigil_memories
        agg.refusals += r.refusals
        agg.promptid_drift = agg.promptid_drift or r.promptid_drift
        bytes_read += st.st_size
    return agg
