# ccmem — design brief

Input for `/superpowers:brainstorm`. The Settled section is constraint, not proposal.
The Open questions section is what I want help with.

## Problem

Every Claude Code session starts cold. I re-explain the same architectural decisions,
re-state the same preferences, and re-discover the same gotchas. CLAUDE.md covers the
stable stuff but it's a hand-maintained file that goes stale and bloats. Auto memory
(`MEMORY.md`) helps but loads only the first ~200 lines and does no retrieval — it's a
file, not a memory system.

What's missing is the episodic layer: *what did we decide last Tuesday and why*.

## Who it's for

Me, first. One developer, many repos, heavy Claude Code use, allergic to infrastructure
I have to babysit.

## Settled decisions

These come out of a survey of ~15 existing memory projects (claude-mem,
claude-self-reflect, KoretyAutomate/claude-memory, Basic Memory, mem0, Zep/Graphiti,
Letta, Cipher, fortytwo/memory, memento, xmemory, knowledge-rag, Hindsight, the
official MCP knowledge-graph server, and the Cline Memory Bank pattern). Rationale
included so you can argue if FACTS.md contradicts something.

**S1 — Hooks are the primary mechanism, MCP is secondary.**
MCP memory tools are model-invoked. Every project that relies on them reports the
model forgetting to call them. Hooks fire deterministically. We use hooks for the
mechanical path (capture every turn, inject every session) and expose an MCP/skill
`ccmem_search` for explicit on-demand recall.

**S2 — SQLite, single file, no server.**
claude-self-reflect started on Qdrant and moved toward a single local binary. That
direction is the tell. FTS5 gives sub-millisecond lexical search; `sqlite-vec` adds
vectors into the same file when we need them. Backup is `cp mem.db`.

**S3 — Inject at SessionStart, not per turn.**
SessionStart output lands in the stable prompt prefix: one cache write per session.
`UserPromptSubmit` injection that varies per turn invalidates the cache from that
point on — there's a documented incident of ~$45 burned this way on a long session.
Per-turn retrieval stays behind a flag, default off.

**S4 — Capture is async relative to the hook.**
`Stop` fires on every turn. It enqueues and returns in under ~50ms. Summarisation
and embedding happen out of band. `SessionEnd` shares a tight budget (~1.5s default)
so it flushes, it doesn't think.

**S5 — Project-scoped by default, with a scope column.**
`scope` is `project | user | global`, not a separate store. Hindsight's multi-scope
tagging is the right model; claude-self-reflect's separate-project-collections model
produces the "why can't it see my other repo" confusion.

**S6 — Supersession over deletion, with recency decay.**
Stale memory is worse than no memory. Contradicted rows get `status='superseded'`
and a pointer to what replaced them. Ranking uses recency decay so old-but-not-wrong
material fades rather than disappearing.

**S7 — Small K, hard token cap.**
"Lost in the Middle" and Chroma's context-rot work both say more retrieved context
makes recall worse, not better. Default ceiling: 12 memories or ~1200 tokens at
SessionStart, whichever binds first. The cap is configurable but the default is low
on purpose.

**S8 — Local embeddings or none.**
No API key required to run ccmem. FastEmbed ONNX (`all-MiniLM-L6-v2`, 384-dim) or
Ollama `nomic-embed-text`. If neither is present, degrade to FTS5-only rather than
failing.

**S9 — Ships as a Claude Code plugin.**
`plugin/.claude-plugin/plugin.json` + `hooks/hooks.json` + a skill. One install
command, hooks travel with it, uninstall is clean.

## What we store

- **Decisions** — "chose RLS triggers over app-layer rate limiting because X"
- **Preferences** — "prefers targeted patches over full-file rewrites"
- **Corrections** — things Claude got wrong and was corrected on
- **Project state** — what's in flight, what's blocked, what's next
- **Gotchas** — "port 8076 is closed on that host, use SSH single-mode"

## What we do NOT store

- Transcript mirrors. Agentic search already reads the live code.
- Anything derivable from the repo: file structure, function signatures, deps.
- Secrets, `.env` contents, anything in `<private>` tags.
- Generic model knowledge.

## Failure modes to design against

Drawn from the issue trackers and READMEs of existing projects:

| Failure | Seen in | Design response |
|---|---|---|
| Hook script missing → input blocked entirely | claude-code #64223 | R1: wrapper exits 0 on anything |
| API cost of AI summarisation | claude-mem #618 | Local-first; summariser optional, off by default |
| Cache busting from varying injected text | claude-code #29963, #83913 | R5, R6; cache-safety gate |
| Windows pipe/printf breakage in UserPromptSubmit | claude-mem #2604 | Per-turn off by default; no shell pipelines in hooks |
| Cross-project scope confusion | claude-self-reflect #27 | S5: scope column, explicit in the injected block |
| Embedding dimension mismatch on upgrade | claude-self-reflect history | Store dim in schema, refuse mixed-dim reads |
| Semantic collision between contradictory facts | vector-store systems generally | S6 supersession, explicit conflict detection |
| Memory as a prompt-injection vector | xmemory design notes | R9: delimited, labelled as data |

## Open questions — this is what I want help with

**Q1 — What actually triggers a write?**
Every `Stop` is too noisy; only `SessionEnd` loses in-session decisions. Options:
heuristic classifier on the turn, explicit `#`-style user marking, an LLM pass at
session end over the whole transcript, or some hybrid. What's the cheapest thing
that captures the decisions and skips the noise?

**Q2 — How do we detect contradiction without an LLM in the write path?**
S6 needs conflict detection. Embedding-similarity-above-threshold plus a recency
tiebreak is the cheap version, but it'll false-positive on "similar topic, both
true." Is there something better that stays local and fast?

**Q3 — What's the right unit of memory?**
One fact per row? A session summary per row? A decision with its context as a row?
This determines whether retrieval returns something useful or something fragmentary.

**Q4 — Multi-repo scoping.**
I work across many repos and some are monorepos. Is scope keyed on git remote, repo
root, nearest package dir, or `cwd`? Worktrees complicate this. What breaks least?

**Q5 — How does ccmem coexist with auto memory (`MEMORY.md`) and CLAUDE.md?**
Three systems now write durable context. Does ccmem write *into* `MEMORY.md`,
replace it, or sit beside it? Overlap means the same fact gets injected twice and
the token budget doubles for no gain.

**Q6 — What does the user-facing surface look like?**
I need to inspect, correct, and delete memories without opening SQLite. CLI, a
`/ccmem` slash command, a skill, plain markdown export, some combination?

**Q7 — What's the honest measurement of whether this works?**
Retrieval metrics (Recall@K, MRR) measure the wrong thing if the memories themselves
are bad. What's the smallest end-to-end signal that would tell me this is earning
its token cost?
