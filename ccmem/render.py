from __future__ import annotations
from datetime import datetime, timezone
from ccmem.retrieval import Memory

MARKER_OPEN = "<ccmem-memories>"
MARKER_CLOSE = "</ccmem-memories>"


def _age_str(created_at: str) -> str:
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)  # ccmem: cache-safe — age from stored timestamp
        age = (now - dt).days
        if age < 1:
            return "<1d"
        if age < 7:
            return f"{age}d"
        if age < 30:
            return f"{age // 7}w"
        return f"{age // 30}mo"
    except Exception:
        return "?"


def _ngrams(text: str, n: int = 5) -> set[str]:
    t = "".join(c.lower() for c in text if c.isalnum() or c.isspace())
    if len(t) < n:
        return set()
    return {t[i : i + n] for i in range(len(t) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _dedup(memories: list[Memory], external_lines: list[str], threshold: float = 0.4) -> list[Memory]:
    ext = [_ngrams(line) for line in external_lines if line.strip()]
    out = []
    for mem in memories:
        ng = _ngrams(mem.content)
        if any(_jaccard(ng, e) > threshold for e in ext):
            continue
        out.append(mem)
    return out


def render(
    memories: list[Memory],
    project_root: str,
    source: str,
    external_lines: list[str] | None = None,
    max_tokens: int = 1200,
) -> str:
    if not memories:
        return ""
    if external_lines:
        memories = _dedup(memories, external_lines)
    if not memories:
        return ""

    lines: list[str] = []
    token_est = 0
    for mem in memories:
        line = f"- [{mem.type} | {_age_str(mem.created_at)}] {mem.content}"
        token_est += (len(line) + 3) // 4
        if token_est > max_tokens:
            break
        lines.append(line)

    count = len(lines)
    noun = "memory" if count == 1 else "memories"
    header = f"{MARKER_OPEN}\n<!-- {count} {noun} | project: {project_root} | session: {source} -->"
    return f"{header}\n" + "\n".join(lines) + f"\n{MARKER_CLOSE}"
