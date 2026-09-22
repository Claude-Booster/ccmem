# PLAN-SKELETON.md

Phase boundaries and acceptance gates. `/superpowers:write-plan` expands this into
`docs/PLAN.md` with tasks and tests. Don't reorder the phases — each one exists so
the next can't start until something works end to end.

## Reference architecture

```
                         claude (CLI session)
                                 │
   SessionStart ─────────────────┤ inject top-K memories via additionalContext
                                 │   (stable prefix → one cache write per session)
   UserPromptSubmit ─────────────┤ [CCMEM_PER_TURN=1 only] byte-stable retrieval
                                 │
        ┌── agentic loop ────────┤
        │                        │
   Stop ─────────────────────────┤ enqueue turn → return in <50ms
   PreCompact ───────────────────┤ snapshot about-to-be-lost context
   SessionEnd ───────────────────┤ flush queue, close session row
                                 │
                    ┌────────────┴─────────────┐
                    │ ccmem/ (in-process)       │
                    │  classify → redact →      │
                    │  dedupe → supersede →     │
                    │  embed (P2+) → write      │
                    └────────────┬─────────────┘
                                 │
              $CCMEM_HOME/mem.db  (SQLite + FTS5 + sqlite-vec)
                                 │
                    ccmem_search  (MCP tool / skill, on-demand recall)
```

## Storage schema (starting point — brainstorm may revise)

```sql
CREATE TABLE memories (
  id             INTEGER PRIMARY KEY,
  scope          TEXT NOT NULL DEFAULT 'project',   -- project|user|global
  project_key    TEXT NOT NULL,                     -- see Q4
  kind           TEXT NOT NULL,                     -- decision|preference|correction|state|gotcha
  body           TEXT NOT NULL,                     -- verbatim, model-facing
  source_session TEXT,
  content_hash   TEXT NOT NULL UNIQUE,              -- sha256(normalised body)
  confidence     REAL NOT NULL DEFAULT 0.5,
  confirmed      INTEGER NOT NULL DEFAULT 0,        -- human-confirmed
  pinned         INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'active',    -- active|archived|superseded
  supersedes     INTEGER REFERENCES memories(id),
  created_at     TEXT NOT NULL,
  last_access_at TEXT,
  access_count   INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE mem_fts USING fts5(
  body, content='memories', content_rowid='id'
);

-- Phase 2 only
CREATE TABLE embedding_meta (model TEXT, dim INTEGER);   -- refuse mixed-dim reads
CREATE VIRTUAL TABLE mem_vec USING vec0(embedding float[384]);

CREATE TABLE capture_queue (
  id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT,
  enqueued_at TEXT, processed_at TEXT
);

CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY, project_key TEXT,
  started_at TEXT, ended_at TEXT, end_reason TEXT
);
```

---

## Phase 0 — Markdown memory bank, no code

**Why first:** validates the *content taxonomy* before any machinery exists. If you
can't say what's worth remembering by hand, retrieval won't save you.

Deliverable: `.claude/rules/` files for stable project knowledge, plus a
`docs/memory-bank/` with `decisions.md`, `preferences.md`, `gotchas.md`,
`state.md`. Wire them in via `@import` or `paths:`-scoped rules.

Use it for two days. Record in `docs/PHASE0-NOTES.md`: what you actually wrote down,
what you wished you'd written down, and what went stale fastest. **That note is the
input to the classifier in Phase 3.**

Gate: `run_gates.py --phase 0` — files exist, non-empty, token cost of the imported
set is under budget, notes file has real content.

---

## Phase 1 — SQLite + FTS5 + SessionStart injection

The minimum thing that beats a markdown file.

- `ccmem/db.py` — schema, migrations, connection with sane pragmas
- `ccmem/redact.py` — secret patterns, `<private>` stripping (R8)
- `ccmem/render.py` — the delimited injection block (R9)
- `hooks/mem_inject.py` — SessionStart, FTS5 top-K by recency + pinned
- `hooks/mem_capture.py` — Stop, enqueue only
- `hooks/mem_finalize.py` — SessionEnd, drain queue with a naive rule-based extractor
- `ccmem/cli.py` — `add`, `list`, `rm`, `pin`, `doctor`, `inject --dry-run`
- Kill switch: `CCMEM_DISABLED=1`

**No embeddings. No LLM. No MCP.** (R3)

Gate: `--phase 1` — hook contract, cache safety, secret hygiene, budget.
**Then install it into your real settings.json and use it for a week.**

---

## Phase 2 — Vectors and hybrid retrieval

Only if Phase 1's keyword recall is measurably insufficient — bring three real failing
queries from your transcripts.

- `sqlite-vec` extension loading, dim recorded in `embedding_meta`
- Local embeddings: FastEmbed ONNX or Ollama, **degrade to FTS5-only if absent** (S8)
- Hybrid retrieval with reciprocal rank fusion + recency decay + pinned boost
- `ccmem_search` exposed as an MCP tool and a skill (S1)

Scoring starting point, from KoretyAutomate/claude-memory:
`0.50·semantic + 0.25·recency + 0.20·log₂(freq) + 0.05·concept`, pinned ×1.5.

Gate: `--phase 2` — adds determinism (same query → same ranking, no float drift in
rendered output) and the mixed-dimension refusal test.

---

## Phase 3 — Classification, supersession, decay

The quality layer. Answers Q1, Q2, Q3 from the brief.

- Classifier deciding what's worth storing (Phase 0 notes are the training signal)
- Optional LLM summariser, **off by default**, budget-capped, local model supported
- Contradiction detection → `supersedes` chain
- Decay and pruning of unaccessed rows
- `PreCompact` snapshotting

Gate: `--phase 3` — supersession correctness, no duplicate content hashes, decay
monotonicity, summariser cost cap respected.

---

## Phase 4 — Plugin packaging and cache telemetry

- `plugin/.claude-plugin/plugin.json`, `hooks/hooks.json`, skill definition
- One-command install; clean uninstall that leaves the DB
- Cache telemetry: log `prompt_cache_likely_expired` / `estimated_cache_write_usd`
  from SessionStart, and read `cache_creation_input_tokens` from transcript JSONL
- Emit in a format ccgate can consume

Gate: `--phase 4` — plugin manifest validates, install/uninstall round-trips, hooks
coexist with an existing SessionStart hook without clobbering it.

---

## Phase 5 — Evaluation

- 15–30 (query → expected memory) pairs derived from real transcripts
- Recall@5, MRR, plus a "was the injected context actually used" check
- A/B: same task, memory on vs off — time to first useful edit, token delta
- Cache cost delta in tokens and dollars

Gate: `--phase 5` — eval harness runs, baseline recorded in `docs/EVAL.md`,
regressions fail the build.

---

## Explicitly deferred

Cross-project global memory beyond the `scope` column; bi-temporal validity
(Zep/Graphiti style) unless contradiction handling proves inadequate; support for
agents other than Claude Code; any cloud sync.
