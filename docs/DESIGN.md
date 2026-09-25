# ccmem — system design

Resolves the open questions from BRIEF.md. Settled decisions are constraints here.
FACTS.md is the ground truth for hook behaviour; this document depends on it.

---

## What the system does

ccmem captures decisions, preferences, corrections, project state, and gotchas from
Claude Code sessions and retrieves them at the start of subsequent sessions. It fills
the gap between CLAUDE.md (stable rules you maintain by hand) and auto memory
(conversational corrections Claude captures from chat): the episodic layer of *what
did we decide last Tuesday and why*.

The mechanical path is fully deterministic: hooks fire, the DB updates, injection
happens. Model calls are optional and off by default until Phase 3.

---

## Architecture overview

```
Session lifecycle        ccmem components            Storage
─────────────────────    ──────────────────────────  ──────────────────
SessionStart hook    ──▶ mem_inject.py: bounded      mem.db (SQLite)
(mem_inject.py)          recovery sweep of project     memories table
                         transcripts → candidates/     candidates table
                         sigil memories/refusals;      session_injections
                         then inject memories +
                         refusal notice
                         → additionalContext

PreCompact hook      ──▶ mem_snapshot.py: capture    candidates (pending,
(mem_snapshot.py)        new turn-pairs from            is_pre_compact=1)
                         transcript → candidates;
                         sigils → memories/refusals

SessionEnd hook      ──▶ mem_flush.py: capture       candidates (pending)
(mem_flush.py)           new turn-pairs from           mem.db
                         transcript → candidates;
                         sigils → memories/refusals;
                         WAL checkpoint

[UserPromptSubmit        REMOVED (2026-09-24) —
 hook — removed]         sigil (!mem:) and per-turn
                         injection opt-in retired;
                         see R5 retirement note

[Stop hook —             REMOVED (2026-09-24) —
 removed]                heuristic scoring now runs
                         over transcript turn-pairs
                         at PreCompact/SessionEnd,
                         not per-turn

Out-of-band          ──▶ [Phase 3] async worker:     memories (extracted)
(async worker or         LLM pass over candidates
ccmem digest)
```

**Plugin entrypoints** (`hooks/`): one thin file per event, each wrapping
`ccmem/` library calls. Hooks exit 0 on any error (R1). Three surviving
hooks: `mem_inject.py` (SessionStart), `mem_snapshot.py` (PreCompact),
`mem_flush.py` (SessionEnd). `mem_retrieve.py` and `mem_capture.py` are
removed.

**Library** (`ccmem/`): `db.py`, `capture.py`, `retrieval.py`, `render.py`,
`redact.py`, `scoping.py`. No business logic in hook files.

---

## Storage schema

```sql
CREATE TABLE memories (
    id          TEXT PRIMARY KEY,     -- uuid4
    type        TEXT NOT NULL,        -- decision|preference|correction|project_state|gotcha
    content     TEXT NOT NULL,        -- injected: the fact, ≤3 sentences
    context     TEXT,                 -- NOT injected: reasoning/backstory, on-demand only
    subject     TEXT,                 -- primary entity, for conflict detection
    scope       TEXT NOT NULL DEFAULT 'project',  -- project|user|global
    project_id  TEXT NOT NULL,        -- sha256(project_root)[:16]
    project_root TEXT NOT NULL,       -- human-readable path, for hand-inspection
    created_at  TEXT NOT NULL,        -- ISO 8601
    accessed_at TEXT,                 -- last retrieval timestamp
    access_count INTEGER DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active',   -- active|superseded|deleted
    supersedes  TEXT REFERENCES memories(id),     -- what this replaced
    embedding   BLOB                  -- NULL until Phase 2 (sqlite-vec)
);

CREATE TABLE candidates (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    prompt_id        TEXT,            -- from hook input field (v2.1.196+)
    user_turn        TEXT NOT NULL,   -- raw user prompt text
    assistant_turn   TEXT NOT NULL,   -- last_assistant_message from Stop
    classifier_score REAL NOT NULL,
    is_pre_compact   INTEGER DEFAULT 0,
    created_at       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'  -- pending|accepted|rejected|extracted
);

CREATE TABLE session_injections (
    session_id  TEXT NOT NULL,
    memory_id   TEXT REFERENCES memories(id),
    injected_at TEXT NOT NULL,
    PRIMARY KEY (session_id, memory_id)
);

-- FTS5 full-text index over content + subject (Phase 1)
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content, subject, context,
    content=memories, content_rowid=rowid
);

-- Schema metadata (detect embedding dimension mismatches on upgrade — §8)
CREATE TABLE schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO schema_meta VALUES ('embedding_dim', '384');  -- NULL until Phase 2
INSERT INTO schema_meta VALUES ('schema_version', '1');
```

**content/context split** (Q3): `content` is what gets injected — the fact itself, no
more than 1–3 sentences, written to fit the 1200-token session budget. `context` is the
reasoning that makes the fact non-obvious; it stays in SQLite and surfaces only when
Claude calls `ccmem_search` or you run `ccmem show <id>`. This is the load-bearing
distinction: you preserve the *why* without paying for it every session.

---

## Q1 — Write path: what triggers a write

### Phase 1 (no LLM in the path)

**Explicit capture** (durable, highest priority):

The user types `!mem: fact text` anywhere in a prompt. The annotation is captured by
reading the transcript JSONL during the snapshot hooks (`mem_snapshot.py` at PreCompact,
`mem_flush.py` at SessionEnd) and the SessionStart recovery sweep (`mem_inject.py`).
The sigil record is written directly to `memories` as type `preference` (redacted; a
secret sigil is refused and recorded in `sigil_refusals`, surfaced at the next
SessionStart injection block). Idempotent via `content_hash` — re-processing the same
sigil record is a no-op.

Sigil is `!mem:` — not `#`. A prompt starting with `#` is Claude Code's add-to-memory
shortcut; using `#` as ccmem's sigil would collide and send the same content to both
CLAUDE.md and ccmem's DB. `!` has no special meaning in Claude Code prompts.

**R5 (per-turn injection opt-in) is retired as of 2026-09-24:** the `UserPromptSubmit`
hook is removed; SessionStart (`mem_inject.py`) is the sole injection path. The
`CCMEM_PER_TURN=1` opt-in and the associated byte-stability requirement are moot —
the only per-turn `additionalContext` emitter is gone. See
`docs/superpowers/specs/2026-09-24-snapshot-driven-capture-design.md`.

**Empirical note (2026-09-22, superseded 2026-09-24):** `claude -p` (print/non-interactive
mode) does not fire UserPromptSubmit hooks — this finding motivated removing the hook.
Gate tests for sigil capture invoke `capture_transcript` directly with a synthesized
transcript JSONL; no live hook payload is needed.

**Heuristic flagging** (at PreCompact/SessionEnd, over transcript turn-pairs):

`capture_transcript` (called by `mem_snapshot.py` and `mem_flush.py`) reads the
project's transcript JSONL and scores each new turn-pair (user + assistant text) not
yet recorded in `transcript_progress`. The same scored regex pass runs on both:

| Pattern (examples) | Points |
|---|---|
| `"we decided"`, `"we're going with"`, `"the approach is"` | 3 |
| `"from now on"`, `"always"`, `"never"`, `"going forward"` | 3 |
| `"you were wrong"`, `"that's incorrect"`, `"actually"` | 2 |
| `"use X instead of Y"`, `"switch to"`, `"replace with"` | 2 |
| Code diff blocks (``` with - and + lines) reversing prior content | 2 |
| `"the reason"`, `"because"`, `"in order to"` (supporting context) | 1 |

Score ≥ threshold (default 4, configurable) → insert raw turn pair into `candidates`
with `status='pending'`. Capture is idempotent via `content_hash UNIQUE` on the
`candidates` table — re-processing the same turn-pair is a no-op.

**Honest assessment of rule-based Phase 1 extraction**: the classifier reliably flags
*that* a turn may contain a decision. It cannot reliably extract *what* the decision
was in structured form — indirect phrasings, multi-step decisions, and decisions
embedded in long reasoning all fall through. Phase 1 therefore does not auto-extract
from flagged turns. Instead, `ccmem review` shows pending candidates and prompts the
user to accept/reject/annotate each. Accepted candidates become structured memories.

This makes Phase 1 "explicit-plus-prompted-review", not "automated capture". That's
honest. Automated extraction moves to Phase 3 with the LLM worker.

**PreCompact hook** (`mem_snapshot.py`): fires before the context is compacted.
Calls `capture_transcript` to score and enqueue any new turn-pairs from the transcript
not yet in `transcript_progress`. Captured rows are marked `is_pre_compact=1` (reserved
for review prioritisation; no consumer built yet). This is a DB write, not an LLM call.
Extraction is always out-of-band (Phase 3).

**SessionEnd hook** (`mem_flush.py`): calls `capture_transcript` for a final capture of
new turn-pairs, then issues a WAL checkpoint. This is the guaranteed end-of-session
flush; PreCompact may not fire in short sessions.

### Phase 3 (async LLM, off by default)

An async worker watches `candidates` for `status='pending'` rows. When triggered (by
`ccmem digest` command or background schedule), it batches pending candidates and
runs a single LLM call per batch. The LLM extracts structured `(type, content, context,
subject)` tuples. Budget cap configurable, default 0 (off). Extracted rows are written
to `memories`; candidates are marked `status='extracted'`.

**Phase 1 depends only on Phase 1 components.** Phase 3's async worker is additive.

---

## Q2 — Contradiction detection

### Phase 1 (no embeddings)

On every write to `memories`:

1. Extract `subject` with a rule-based parser — first code-span (backtick-wrapped),
   first quoted string, first `PORT:NNNN`, first URL hostname, first ALL_CAPS_CONSTANT,
   first proper noun in the first sentence. Return `None` if nothing found.

2. If `subject` is not None: `SELECT * FROM memories WHERE status='active'
   AND project_id=? AND subject=? ORDER BY created_at DESC LIMIT 1`.

3. If a row is found: set its `status='superseded'`, `supersedes=new_id`. The new
   memory is written with `status='active'`.

4. If `subject` is None, or no match: write without supersession. Recency decay
   handles stale facts in ranking.

This is fast (one SQL query per write), deterministic, and requires no embeddings.
It will miss contradictions where subject extraction fails. That is acceptable in
Phase 1; recency decay ensures old undetected contradictions fade in ranking.

### Phase 2 (add KNN path)

After embedding all active memories, add a second supersession path:

1. Embed the new memory's `content`.
2. KNN query: top 3 most similar active memories in the same `project_id + scope`.
3. Similarity > threshold_high → mark older as superseded.
4. Similarity > threshold_mid → insert into a `conflicts` review queue (don't
   auto-supersede — "same topic, both true" lives in this band).

**Thresholds go in config, not source.**

**Calibration plan**: before shipping Phase 2, build a labeled dataset of 20 memory
pairs — 10 true conflicts (newer fact replaces older), 10 same-topic-different-facts.
Run KNN supersession at candidate thresholds and measure precision on conflicts. Target
≥90% precision at `threshold_high`. Write this as a gate (`gate_threshold_calibration`)
that runs against the labeled dataset. "Run it and look" is acceptable for finding the
initial values, but the gate locks them in.

---

## Q3 — Unit of memory: decision record

One row = one discrete fact plus the reasoning that makes it non-obvious.

`content` (injected, 1–3 sentences): the fact itself.  
`context` (not injected, any length): why this fact is true, what was tried before,
what the tradeoffs were. Available on demand via `ccmem show` or `ccmem_search`.

`type` maps to the BRIEF's five categories: `decision | preference | correction |
project_state | gotcha`.

The content/context split is what keeps the 1200-token injection cap meaningful.
Without it, preserving reasoning would require injecting long text on every session.
With it, the session gets the facts and can pull reasoning when needed.

---

## Q4 — Multi-repo scoping

### Empirical finding

`git rev-parse --show-toplevel` inside a linked worktree returns the **worktree's own
path**, not the main repo root. Verified on CCGate (v2.1.278): worktree path was
`/tmp/ccmem-wt-test`; main repo path was `CCGate/`.

`git rev-parse --git-common-dir` returns the main `.git` directory from all worktrees.
Its parent is the main repo root in all cases.

### Resolution

```python
# ccmem/scoping.py

import os, subprocess, hashlib

def resolve_project_root(cwd: str) -> str:
    """Returns git repo root if in a git repo, else cwd. Worktree-safe."""
    try:
        r = subprocess.run(
            ['git', 'rev-parse', '--git-common-dir'],
            cwd=cwd, capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            return cwd
        common = r.stdout.strip()
        # In main repo: common = '.git' (relative to cwd)
        # In linked worktree: common = absolute path to main .git
        abs_common = os.path.normpath(os.path.join(cwd, common))
        return os.path.dirname(abs_common)  # parent of .git = repo root
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return cwd  # no git binary, or timeout: degrade to cwd

def project_key(root: str) -> tuple[str, str]:
    """Returns (project_id, project_root). Both stored in every row."""
    pid = hashlib.sha256(root.encode()).hexdigest()[:16]
    return pid, root
```

Store both columns in every `memories` row: `project_id` (hash, for joins) and
`project_root` (path, for human inspection with `ccmem list`).

All linked worktrees of the same repository share the same `project_id`. This matches
how auto memory works (confirmed, FACTS §6).

**Scope retrieval at SessionStart**:
```sql
WHERE status='active'
  AND (
    scope='global'
    OR scope='user'
    OR (scope='project' AND project_id = :current_project_id)
  )
```

`scope='global'` is for cross-repo facts (infrastructure gotchas, machine-level
preferences). The LLM extraction prompt in Phase 3 marks infrastructure/environment
facts as global; explicit `!mem:` capture uses `global` scope if the user writes
`!mem[global]: text`.

**Monorepo**: git root is the project boundary in Phase 1. Package-level scoping is
out of scope until Phase 1 is validated.

---

## Q5 — Coexistence with auto memory and CLAUDE.md

### System ownership

| System | Owns | Written by | Retrieval |
|---|---|---|---|
| CLAUDE.md | Stable conventions, always-on rules | You, by hand | Loaded in full at startup |
| Auto memory (MEMORY.md) | Conversational corrections, "remember X" preferences | Claude, from chat | First 200 lines of index at startup; topic files on demand |
| ccmem | Episodic decisions, project state, gotchas | Hook-driven structured capture | Ranked retrieval: relevance × recency × frequency |

ccmem does not write to MEMORY.md. They run in parallel.

### Why a separate store (not just MEMORY.md + topic files)

Auto memory now uses the same progressive-disclosure shape as the content/context split
(index in context, detail on demand). That's the right shape. What auto memory lacks:

- **Ranked retrieval**: MEMORY.md loads the first 200 lines in file order — no
  relevance ranking. ccmem selects by FTS5 score (Phase 1) or embedding similarity
  (Phase 2).
- **Supersession**: MEMORY.md is a flat file. ccmem tracks when a fact changes,
  marks the old version superseded, and keeps both for audit.
- **Cross-session scoring**: `access_count` and `accessed_at` track what gets used
  and feed future ranking. MEMORY.md has no usage signal.
- **Multi-scope**: ccmem differentiates `project / user / global`. MEMORY.md is
  per-project only.

If ccmem is working, the MEMORY.md index is redundant for the categories ccmem
covers. But don't disable auto memory — users may have existing content, and the
conversational correction path ("remember X" typed in chat) is lower-friction than
`!mem:` for small preferences.

### Overlap prevention at inject time (deterministic)

Before rendering the injection block, `render.py` runs a deduplication pass:

1. Normalize each candidate memory's `content`: lowercase, strip punctuation,
   split into character 5-grams.
2. Normalize each loaded CLAUDE.md line and each MEMORY.md line similarly.
3. For each candidate, compute Jaccard similarity of its 5-gram set against each
   CLAUDE.md/MEMORY.md line.
4. If max similarity > 0.4: skip this memory for this session's injection.
5. Log skipped memory IDs at debug level.

This is a lexical check, runs in <5ms for typical CLAUDE.md and MEMORY.md sizes,
and is entirely deterministic. It is gateable: `gate_overlap_dedup` seeds a DB with
a memory whose content overlaps a fixture CLAUDE.md and asserts it does not appear
in the injection output.

`ccmem doctor` reports detected overlaps between the active memory store and loaded
MEMORY.md lines, so users can decide whether to delete the duplicate from one system.

---

## Injection format

The SessionStart hook outputs the injection block as plain text to stdout (no JSON
needed unless also setting sessionTitle — FACTS §2). Claude Code appends it to context
alongside other SessionStart hooks' output.

```
<!-- ccmem: 4 memories | project: ~/Documents/ccmem | session: resume -->
[decision | 3d] chose RLS triggers over app-layer rate limiting — latency budget tighter than consistency requirement
[gotcha | 8d] port 8076 is closed on that host; use SSH tunnel to 8077 instead
[preference | 14d] prefers targeted patch edits over full-file rewrites
[project_state | 1d] Phase 1 gates passing; blocked on sqlite-vec Windows build for Phase 2
<!-- /ccmem -->
```

Format per line: `[type | age] content`. Age is in days/weeks/months. Subject is
internal only. Context is not shown.

**Hard limits (S7)**: 12 memories OR 1200 tokens, whichever binds first. If the
rendered block would exceed 10,000 characters, it will be truncated by Claude Code
and replaced with a file path + preview (FACTS §2). Keep individual memories short.

**Delimiter (R9)**: the `<!-- ccmem: ... -->` wrapper marks ccmem's output as recorded
notes, not instructions. This is the prompt-injection defence.

---

## Phase 4 — Adaptive injection using cache telemetry

When `source` is `resume` or `fork`, the SessionStart payload includes
`prompt_cache_likely_expired`, `context_tokens`, and `estimated_cache_write_usd`
(requires v2.1.251+, FACTS §2).

Strategy:
- `prompt_cache_likely_expired=true`: the cache will be rewritten regardless.
  Inject up to the full 1200-token budget — marginal cost is near zero.
- `prompt_cache_likely_expired=false` and `context_tokens` large: cache is warm.
  Inject only the most-accessed memories (reduce budget to ~400 tokens) to avoid
  busting it.
- `source='startup'`: no cache context. Inject at full budget.

This logic lives in `render.py` and is gated by the `gate_cache_adaptive_injection`
test.

**Phase 4 depends on Phase 1's SessionStart hook.** The telemetry fields are parsed in
Phase 1 but the adaptive logic is not applied until Phase 4.

---

## Q6 — User-facing surface

### Phase 1 (CLI)

```bash
python -m ccmem.cli list [--scope project|user|global] [--type decision|...]
python -m ccmem.cli show <id>           # shows content + context
python -m ccmem.cli delete <id>         # status='deleted', reversible
python -m ccmem.cli restore <id>
python -m ccmem.cli review              # pending candidate review loop
python -m ccmem.cli inject --dry-run    # shows what would be injected + token count
python -m ccmem.cli export [--format md|json]
python -m ccmem.cli doctor              # health check (R1 compliance, MEMORY.md overlap)
```

`delete` marks `status='deleted'`, not a hard delete (S6). `restore` reverts it.

`review` shows pending candidates from the `candidates` table one at a time:
```
[2026-09-22 14:31] classifier_score: 5
User: "that's wrong, we're using RLS triggers not app-level"
Claude: "You're right — I'll use RLS triggers from now on..."
Accept (a), Reject (r), Edit (e), Skip (s)? 
```
Accepted candidates become memories; rejected ones are marked `status='rejected'`.

### Phase 3 (/ccmem skill)

In-session recall: `@ccmem search <query>` returns top-K memories as structured text.
In-session annotation: `@ccmem note: <text>` writes directly to `memories` (same path
as `!mem:`).

### Phase 4+ (MCP ccmem_search)

MCP tool enabling Claude to search the store on demand. Input: `query: str`.
Output: top-K `content` fields of active memories matching the query.

---

## Q7 — Measurement

### Phase 1 (manual, no infrastructure)

Before shipping, pick 5 decisions or gotchas from recent sessions that required
re-explanation. Add them to ccmem manually using `ccmem.cli`. Run 10 sessions as
normal. After each session, note whether each of the 5 came up and whether it needed
re-explanation.

Target: 4 of 5 did not need re-explanation. If fewer pass, the injection is not
surfacing memories at the right moment — investigate ranking, scope, or token cap.

This test requires no code beyond the Phase 1 CLI and costs 10 sessions of normal use.

### Phase 5 (automated, requires Phase 2 embeddings)

**Re-explanation rate detector**: for each session, compare embedding similarity
between injected memories (`session_injections`) and user turns in that session.
If a user turn is semantically close to an injected memory and contains a corrective
pattern (`"actually"`, `"that's wrong"`, `"wait, no"`, `"I already told you"`),
mark the injection as ineffective for that session.

`ccmem doctor --eval` reports:
```
Sessions: 23  Injections: 187  Ineffective: 14 (7.5%)
Most ineffective: [preference | 45d] prefers targeted patches (6 misses)
```

Build with the rest of the eval harness in Phase 5. Not before — it requires
embeddings and the `session_injections` table to have real data.

---

## Phase plan

| Phase | Deliverable | New capabilities | Dependencies |
|---|---|---|---|
| 0 | Scaffolding | Repo structure, gates framework, plugin manifest, fixture DB, pyproject.toml | None |
| 1 | Core loop | SQLite + FTS5, snapshot capture at PreCompact/SessionEnd (transcript turn-pairs), explicit `!mem:` capture from transcript, SessionStart injection + recovery sweep, basic CLI, manual candidate review | Phase 0 |
| 2 | Semantic retrieval | sqlite-vec, FastEmbed ONNX (384-dim), KNN retrieval, KNN supersession path | Phase 1 schema (embedding col nullable) |
| 3 | LLM extraction | Async worker, budget cap, `ccmem digest`, `/ccmem` skill | Phase 1 candidates table |
| 4 | Cache telemetry | Adaptive injection using resume/fork fields, session_injections table | Phase 1 SessionStart hook |
| 5 | Eval harness | Re-explanation rate detector, `ccmem doctor --eval` | Phase 2 embeddings + Phase 4 injection tracking |

### Per-turn cost and the Stop hook problem

> **SUPERSEDED (2026-09-24):** resolved by snapshot-driven capture — capture moved off
> the per-turn path to PreCompact/SessionEnd/SessionStart, eliminating per-turn spawns
> entirely. The analysis below is retained for history. See
> `docs/superpowers/specs/2026-09-24-snapshot-driven-capture-design.md`.

Stop fires on every turn. On Windows with hooks living inside a cloud sync boundary
(OneDrive), each Stop spawn costs 3–5s. This is the main Phase 1 friction risk —
not data loss (the 10s timeout addresses that), but a pause on every turn that makes
the tool annoying enough to stop using.

Three options in cost order, recorded here before any of them is committed to:

**Option A — Defender path exclusions + proper plugin install** (cheapest, already
partially done): Defender path exclusions cut spawn cost ~37%. Moving hooks to
`~/.claude/plugins` (outside sync boundary) eliminates the filter-driver overhead
entirely. Combined, these may reduce Stop latency to ~600–900ms, which is acceptable.
Requires one admin PowerShell command (exclusions) and a proper install step. No
architecture change. Try this first.

**Option B — Reduce Stop firing frequency**: Stop hook does nothing (returns
immediately). Candidates are captured only at PreCompact and SessionEnd. This
eliminates the per-turn Stop cost entirely at the price of in-session granularity —
if a session ends badly (crash, forced kill) before PreCompact fires, the session's
candidates are lost. Manual `!mem:` capture still works. Zero architecture change;
the Stop hook becomes a one-liner (`return`). Cost: candidates are captured at
session level, not turn level. This is the right option if Option A doesn't get Stop
latency below ~1s.

**Option C — Pre-warmed Python daemon**: A persistent Python process accepts hook
payloads over a local socket, eliminating OS spawn per turn entirely. Sub-millisecond
per turn for all hooks. High implementation complexity (process lifecycle, crash
recovery, IPC). Justified only if both Option A and Option B are rejected. Gate on
measured drop rate (>5% Stop/UPS in hook_log) or persistent spawn floor above
`spawn_daemon_ms` in `gates/config.json` after Options A+B are applied.

PHASE1-NOTES.md will determine which option (if any beyond A) is needed. Don't commit
to B or C until a week of real sessions has produced hook_log data.

---

**Phase 1 exit criteria** (gate_phase1_notes is the checkpoint):

Phase 1 starts with tens of hand-written memories. Ranking, top-K, and the token budget are not meaningfully exercised until Phase 3 has enough memories to make truncation real. The exit question is friction, not correctness: *did explicit capture and the review loop feel light enough that I actually used it for a week?*

Write `docs/PHASE1-NOTES.md` during the week. Record what you captured, what you wished you'd captured, and what went stale fastest. This file is the empirical input to Phase 3's LLM extractor. The notes file absorbs the intent of the skeleton's Phase 0 (find out what's worth remembering before automation hides the question) — `!mem:` into SQLite is the same exercise with a better store.

The labeled 20-pair conflict dataset for `gate_threshold_calibration` should be built from real Phase 1 transcripts during this week. Phase 2 gates won't pass until it exists.

### Cross-phase dependency notes

- Phase 2 adds an `embedding` column (nullable) to the Phase 1 schema. No migration
  is needed — the column is already present, just NULL.
- Phase 3's LLM worker is additive to Phase 1. Phase 1 ships without it.
- Phase 4 reads fields that Phase 1's SessionStart hook already receives. The adaptive
  logic is a new code path in `render.py`, not a schema change.
- Phase 5's re-explanation detector cannot be built before Phase 2 provides embeddings.
  `session_injections` table is created in Phase 4 and populated from that point on.
- **KNN supersession (Phase 2) does not replace exact-subject supersession (Phase 1).**
  Both paths remain active. Phase 1 handles the common case (same named entity);
  Phase 2 handles semantic conflicts the subject extractor missed.

---

## Gates (incomplete — gates/ will expand each phase)

| Gate | Enforces | Phase |
|---|---|---|
| `gate_schema_contract` | Every table and column in DESIGN.md schema exists after `db.migrate()`; gate column references are internally consistent | 1 |
| `gate_hook_exit_zero` | Every hook exits 0 on malformed stdin, missing DB, and import error | 1 |
| `gate_hook_contract` | SessionStart output ≤10,000 chars; JSON wraps correctly | 1 |
| `gate_fts5_retrieval` | FTS5 query on seeded DB returns expected memories | 1 |
| `gate_injection_format` | Injection block renders with correct delimiter, type labels, age format | 1 |
| `gate_cache_safety` | Per-turn injection (CCMEM_PER_TURN=1) output is byte-stable across repeated calls | 1 |
| `gate_overlap_dedup` | Memory overlapping fixture CLAUDE.md is suppressed at inject time | 1 |
| `gate_subject_supersession` | Writing a memory with matching subject marks the older active row superseded | 1 |
| `gate_worktree_scoping` | resolve_project_root returns same value for main checkout and linked worktree | 1 |
| `gate_budget` | Injected context within token and top-K caps (content injected; context excluded) | 1 |
| `gate_secret_hygiene` | Secrets redacted on write; content + context both audited; DB gitignored | 1 |
| `gate_phase1_notes` | docs/PHASE1-NOTES.md exists with ≥200 words covering what was captured, what was missed, and what went stale — input to Phase 3 | 1 |
| `gate_threshold_calibration` | KNN supersession hits ≥90% precision on labeled conflict dataset (20 pairs built from Phase 1 transcripts) | 2 |
| `gate_embedding_dim_guard` | Read refuses memories with mismatched embedding_dim (stored in schema_meta) | 2 |
| `gate_cache_adaptive_injection` | Injection budget shrinks when prompt_cache_likely_expired=false and context large | 4 |

---

## Redaction

Applied on write, not on read (R8). `ccmem/redact.py` strips before anything reaches
the `memories` table:
- Secrets matching known patterns (API keys, tokens, connection strings)
- `.env` file contents
- Anything inside `<private>` tags in the text
- Anything matching user-configurable regex patterns

Redaction is not reversible. If redaction fires on a `!mem:` annotation, the write is
rejected and the user is told why.
