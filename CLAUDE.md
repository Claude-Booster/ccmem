# ccmem

Persistent memory for Claude Code. Deterministic hook-driven capture and injection,
backed by one local SQLite file. Ships as a Claude Code plugin.

## Commands

```bash
python gates/run_gates.py              # all gates
python gates/run_gates.py --phase 1    # gates for a phase
python -m pytest tests/ -q             # unit + fixture tests
python -m ccmem.cli doctor             # health check against a real ~/.claude
python -m ccmem.cli inject --dry-run   # show what would be injected, with token count
```

## Rules

These are enforced by `gates/`. If a gate and this file disagree, the gate wins.

**R1 — Hooks never break my session.** Every hook script exits 0 on any error:
malformed stdin, missing DB, locked DB, import failure, timeout. A memory tool that
can wedge the CLI is worse than no memory tool. `mem_retrieve.py` must never exit 2.

**R2 — One SQLite file. No server, no daemon.** `$CCMEM_HOME/mem.db`, default
`~/.claude/ccmem/`. Vectors via `sqlite-vec` loaded as an extension into the same
file. No Chroma, no Qdrant, no Postgres, no background process I have to start.

**R3 — Earn complexity.** FTS5 before embeddings. Embeddings before an LLM
summariser. Each layer ships and gets used before the next one starts.

**R4 — Injection happens at SessionStart.** That lands in the stable prompt prefix
and costs one cache write per session.

**R5 — Per-turn injection is opt-in and byte-stable.** `UserPromptSubmit` injection
is off unless `CCMEM_PER_TURN=1`. When on, output for a given prompt must be
byte-identical across repeated calls — no timestamps, no UUIDs, no scores rendered
to floating point, no dict-iteration ordering. Varying text at that position
invalidates the prompt cache for the rest of the conversation.

**R6 — Never inject `additionalContext` from tool-level hooks.** `PreToolUse` and
`PostToolUse` re-serialise between turns and bust cache even when content is
unchanged. Those events are for side effects only.

**R7 — Store decisions, not transcripts.** Memory holds things that can't be
rediscovered from the code: why we chose X over Y, my preferences, corrections,
in-flight project state. Claude Code's agentic search handles the rest.

**R8 — Redact on write, not on read.** Secrets, `.env` contents, anything inside
`<private>` tags, and anything matching the patterns in `ccmem/redact.py` never
reach the `memories` table. Retrieval must not be the thing standing between a
leaked key and my context window.

**R9 — Memory content is untrusted.** It re-enters the context window verbatim.
Wrap injected memories in a delimited block and label them as recorded notes, not
instructions.

**R10 — Supersede, don't delete.** Contradicted memories get
`status='superseded'` and a `supersedes` pointer. I want to be able to see what
changed its mind.

## Working style

- **Patch, don't replace.** Use targeted edits. I often have edits in flight in the
  same files; a full-file rewrite silently discards them.
- **Gates over prose.** A rule that isn't a runnable check is a suggestion. If you
  find yourself writing a convention into a doc, ask whether it belongs in `gates/`.
- **Show me the token cost.** Any change that alters what gets injected reports the
  before/after token count in the PR summary.
- **Python 3.11+, stdlib-first.** Third-party deps need a reason. `sqlite-vec` and
  the embedding runtime are the expected exceptions.

## Layout

```
ccmem/            library: db, retrieval, capture, redact, render
hooks/            hook entrypoints, one per event, thin wrappers over ccmem/
gates/            executable acceptance criteria — DO NOT EDIT to make tests pass
fixtures/         synthetic hook payloads and a seeded DB
plugin/           .claude-plugin manifest, hooks.json, skill
tests/            pytest
docs/             BRIEF, FACTS, DESIGN, PLAN, EVAL
```

## Interop

Superpowers and other plugins also register SessionStart hooks. Hooks merge rather
than replace across settings levels, so ccmem must assume it is one of several and
must not assume its output is the only thing injected. Keep the injected block
clearly delimited.

## Non-goals

- Cloud sync, multi-user, team-shared memory.
- Re-indexing the codebase for semantic code search. Different problem.
- Working with agents other than Claude Code. Not until it's good here first.
