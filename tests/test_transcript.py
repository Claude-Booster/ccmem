"""Tests for ccmem.transcript — transcript-parsing module."""
import os
import pytest
from ccmem.transcript import iter_turn_pairs, normalize_transcript_path

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "transcripts")


def test_pairs_and_apiblock_concat():
    pairs = list(iter_turn_pairs(os.path.join(FIX, "basic.jsonl")))
    assert [p.prompt_id for p in pairs] == ["p1", "p2"]
    assert [p.ordinal for p in pairs] == [0, 1]
    assert pairs[1].assistant_turn == "the approach is X because it is simpler"
    assert pairs[1].user_turn == "we decided to use X instead of Y"


def test_truncated_final_line_skipped():
    pairs = list(iter_turn_pairs(os.path.join(FIX, "truncated_tail.jsonl")))
    # p1 pair is complete and captured; p2's partial line is skipped
    assert [p.prompt_id for p in pairs] == ["p1"]


def test_normalize_collapses_spellings(tmp_path):
    f = tmp_path / "T.jsonl"
    f.write_text("{}")
    a = normalize_transcript_path(str(f))
    b = normalize_transcript_path(str(f).upper() if os.name == "nt" else str(f))
    assert a == b


def test_robust_to_crlf_and_non_utf8(tmp_path):
    """Parser must skip CRLF lines and non-UTF-8 bytes without aborting the file.

    A valid turn-pair BEFORE and AFTER a bad line must still be yielded.
    This locks the behavior required by the plan's Review Focus (R1 spirit).
    """
    # Build the file bytes manually so we can inject raw non-UTF-8 bytes.
    good_user1 = (
        b'{"type":"user","promptId":"r1","cwd":"C:\\\\proj",'
        b'"message":{"content":[{"type":"text","text":"first"}]}}\r\n'
    )
    good_asst1 = (
        b'{"type":"assistant","apiBlockIndex":0,'
        b'"message":{"content":[{"type":"text","text":"reply one"}]}}\r\n'
    )
    # A line containing a raw non-UTF-8 byte (0xff) — not valid JSON either
    bad_line = b"not json \xff garbage\r\n"
    good_user2 = (
        b'{"type":"user","promptId":"r2","cwd":"C:\\\\proj",'
        b'"message":{"content":[{"type":"text","text":"second"}]}}\r\n'
    )
    good_asst2 = (
        b'{"type":"assistant","apiBlockIndex":0,'
        b'"message":{"content":[{"type":"text","text":"reply two"}]}}\n'
    )

    path = tmp_path / "mixed.jsonl"
    path.write_bytes(good_user1 + good_asst1 + bad_line + good_user2 + good_asst2)

    pairs = list(iter_turn_pairs(str(path)))
    assert [p.prompt_id for p in pairs] == ["r1", "r2"], (
        "Both turn-pairs must be yielded despite bad line between them"
    )
    assert pairs[0].assistant_turn == "reply one"
    assert pairs[1].assistant_turn == "reply two"
