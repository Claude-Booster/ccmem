from ccmem.retrieval import Memory
from ccmem.render import render, MARKER_OPEN, MARKER_CLOSE


def _mem(id="m1", type="decision", content="chose RLS triggers",
         subject="rate-limiter", scope="project",
         created_at="2026-09-01T00:00:00Z", access_count=0):
    return Memory(id, type, content, subject, scope, created_at, access_count)


def test_render_contains_open_marker():
    out = render([_mem()], "/repo", "startup")
    assert MARKER_OPEN in out


def test_render_contains_close_marker():
    out = render([_mem()], "/repo", "startup")
    assert MARKER_CLOSE in out


def test_render_contains_content():
    out = render([_mem(content="chose RLS triggers")], "/repo", "startup")
    assert "chose RLS triggers" in out


def test_render_empty_memories():
    out = render([], "/repo", "startup")
    assert out == ""


def test_render_dedup_against_external():
    external = ["chose RLS triggers over app-layer limits"]
    out = render([_mem(content="chose RLS triggers over app-layer limits")],
                 "/repo", "startup", external_lines=external)
    assert "chose RLS" not in out


def test_render_respects_token_cap():
    mems = [_mem(id=f"m{i}", content=f"fact {i}: " + "x" * 100) for i in range(300)]
    out = render(mems, "/repo", "startup", max_tokens=1200)
    assert len(out) < 6000


def test_render_format_per_line():
    out = render([_mem(type="decision", content="chose RLS")], "/repo", "startup")
    assert any(line.startswith("- [decision") for line in out.splitlines())


def test_render_is_deterministic():
    mems = [_mem()]
    out1 = render(mems, "/repo", "startup")
    out2 = render(mems, "/repo", "startup")
    assert out1 == out2
