import os
from ccmem.killswitch import mark_disabled, is_disabled, session_id_from_transcript

def test_mark_then_detect_by_session_id(tmp_path):
    home = str(tmp_path)
    assert is_disabled(home, "sess-uuid-1") is False
    assert mark_disabled(home, "sess-uuid-1") is True
    assert is_disabled(home, "sess-uuid-1") is True

def test_session_id_from_filename():
    assert session_id_from_transcript(
        os.path.join("x", "projects", "munged", "abc-123.jsonl")) == "abc-123"

def test_mark_returns_false_when_unwritable(tmp_path, monkeypatch):
    # point home at a path that cannot be created (a file, not a dir)
    bad = tmp_path / "not_a_dir"; bad.write_text("x")
    assert mark_disabled(str(bad), "s1") is False
