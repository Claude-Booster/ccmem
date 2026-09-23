# FACTS.md — Claude Code mechanics ccmem depends on

<!--
Compiled 2026-09-22. Claude Code ships roughly weekly; treat everything here as
stale until Prompt 1 re-verifies it.

status values:
  verified    — confirmed against official docs at compile time
  verify      — needs re-checking before you rely on it
  community   — from a third-party source, not official docs
  unconfirmed — could not find a primary source
-->

**Verified against Claude Code version:** 2.1.278
**Last verification run:** 2026-09-22

---

## §1 — Hook events and which ones reach the model

`status: verified` — source: https://code.claude.com/docs/en/hooks

The decisive rule: for most hook events, stdout goes to the debug log and is not
shown to the model. The documented exceptions are **`UserPromptSubmit`,
`UserPromptExpansion`, `SessionStart`, and `PostModelSwitch`** — for these, plain-text
stdout is added as context the model can see.

**Consequence for ccmem:** SessionStart and UserPromptSubmit are the only viable
injection points. Everything else is capture/side-effect.

Note: as of v2.1.278 there are 30+ hook events total; only the ccmem-relevant ones
are listed here.

Events relevant to us:

| Event | Role in ccmem | Can inject? | Can block? |
|---|---|---|---|
| `SessionStart` | primary injection | yes | yes (exit 2 cancels startup — we must never) |
| `UserPromptSubmit` | opt-in per-turn injection | yes | yes (exit 2 erases prompt — we must never) |
| `Stop` | per-turn capture | no | yes (exit 2 continues conversation — we must not) |
| `SessionEnd` | flush/finalise | no | yes (exit 2 cancels session exit — we must never) |
| `PreCompact` | snapshot before context loss | no | yes (exit 2 cancels compaction — we must not) |
| `PreToolUse` / `PostToolUse` | **do not use for injection** — see §5 | — | — |

---

## §2 — Hook payload and output schemas

`status: verified` — source: https://code.claude.com/docs/en/hooks

Common stdin fields on all events (v2.1.278): `session_id`, `transcript_path`, `cwd`,
`hook_event_name`. Additional fields added in recent versions: `prompt_id` (v2.1.196+,
UUID matching the current prompt — absent until first user input), `scratchpad_dir`
(v2.1.257+, path to session's scratchpad), `permission_mode` (current mode:
`"default"`, `"plan"`, `"acceptEdits"`, `"auto"`, `"dontAsk"`, or
`"bypassPermissions"`), `effort` (object with `level` field: `"low"` … `"max"`).

**SessionStart** — adds `source` (`startup` | `resume` | `clear` | `compact` | `fork`),
optional `model`, `agent_type`, `session_title`. When `source` is `resume` or `fork`
and the transcript has at least one Claude response, four additional fields are present
(`status: verified`, requires v2.1.251+):

| Field | Description |
|---|---|
| `seconds_since_last_response` | Wall-clock seconds since the last response in the resumed transcript |
| `context_tokens` | Tokens the first request re-sends as its prompt |
| `prompt_cache_likely_expired` | `true` when last response is older than the cache TTL, or a compaction replaced it |
| `estimated_cache_write_usd` | Estimated USD to write `context_tokens` to the prompt cache |

> **Phase 4 hook:** when `prompt_cache_likely_expired=true`, injecting more context has
> near-zero marginal cost (cache will be rewritten regardless). When it's `false`,
> minimize injection to avoid busting a warm cache.

**`scratchpad_dir` field** (`status: verified`, v2.1.257+): path to the session's
scratchpad — a per-session ephemeral directory under the system temp path. Lives at
`<TEMP>/claude/<project>/<session-id>/scratchpad`. It is **not** appropriate for
persistent queues that must survive session boundaries.

Output:
```json
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "…",
    "sessionTitle": "optional"
  }
}
```

**UserPromptSubmit** — adds `prompt`. Output:
```json
{
  "decision": "block",
  "reason": "shown to user, NOT added to context",
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "…"
  }
}
```
Cannot replace the prompt, only prepend. **Exit 2 blocks the prompt and erases it.**
Default timeout is 30s (shorter than the 600s most events get); on timeout the output
including `additionalContext` is discarded and the prompt proceeds without it.

**Stop** — adds `stop_hook_active` (true when already continuing because of a prior
Stop block — check it and exit 0 or you loop) and `last_assistant_message` (use this
instead of re-parsing the transcript for the final turn).

**SessionEnd** — adds `reason` (`clear` | `resume` | `logout` | `prompt_input_exit` |
`other`). No injection, no JSON `decision` field. **Can block via exit 2** (cancels
session exit) — exit-code blocking and JSON decision-field control are independent axes;
SessionEnd has the former, not the latter. `status: verified` — shares a ~1.5s budget
across SessionEnd hooks, raisable via an explicit `timeout` up to 60s.

**PreCompact** — adds `trigger` (`manual` | `auto`) and `custom_instructions`.

**Universal output fields:** `continue: false` (+ `stopReason`) halts Claude;
`systemMessage` surfaces a warning to the user; `suppressOutput`. `status: verified` —
`additionalContext`, `systemMessage`, and plain stdout are each capped at 10,000
characters. Overflow: Claude Code writes the full text to a file in the session
directory and passes Claude the path plus a short preview.

**Injection shortcut (SessionStart):** plain stdout also works for context-only
injection without JSON. Use the JSON form only when also setting `sessionTitle`. When
multiple SessionStart hooks each emit `additionalContext`, Claude receives all values
concatenated.

---

## §3 — settings.json hook configuration

`status: verified` — source: https://code.claude.com/docs/en/settings

Hooks **merge** across settings levels (user / project / local) rather than replacing.
ccmem must assume other plugins — superpowers included — also register SessionStart.

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "startup|resume",
        "hooks": [{ "type": "command",
                    "command": "${CLAUDE_PROJECT_DIR}/hooks/mem_inject.py",
                    "timeout": 10 }] }
    ]
  }
}
```

`status: verified` — comma-separated matchers confirmed v2.1.191+. Hyphen exact-match
(v2.1.195+): `status: unconfirmed` — docs show hyphens are in the exact-match
character set but do not cite a version.

---

## §4 — Transcript JSONL

`status: community` — schema is explicitly undocumented; anthropics/claude-code #53516
requests documentation. Treat as unstable and build a drift-tolerant parser.

Location: `~/.claude/projects/<munged-cwd>/<session-uuid>.jsonl`, where the cwd's
path separators become dashes. Append-only, one JSON object per line. Resuming appends
to the same file.

**Worktree empirical finding (2026-09-22, v2.1.278):** `git rev-parse --show-toplevel`
inside a linked worktree returns the worktree's own directory, NOT the main repo root.
Use `git rev-parse --git-common-dir` instead; its parent is always the main repo root
across all worktrees. Auto memory uses the git root for the project directory (confirmed
in §6), so the same resolution is correct for ccmem's `project_id`.

Observed top-level record types (v2.1.278): `user`, `assistant`, `system`, `summary`
(as before), plus `queue-operation` and `attachment` (new infrastructure records;
build a drift-tolerant parser that ignores unknown types).

Observed top-level fields on user records: `type`, `uuid`, `parentUuid`, `timestamp`,
`sessionId`, `cwd`, `gitBranch`, `version`, `isSidechain`, `userType`, `promptId`,
`permissionMode`, `origin`, `promptSource`, `turnOrigin`, `entrypoint`.

Observed top-level fields on assistant records: above plus `requestId`, `apiBlockIndex`,
`effort`, `perTurnEffort`, `wireToolInputs`. Each content block is a separate record
(keyed by `apiBlockIndex`).

`message.content` holds blocks: `text`, `thinking`, `tool_use` (`id`, `name`, `input`),
`tool_result` (`tool_use_id`).

Token accounting in `message.usage`: `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens` are still present at the top
level of `usage`. **These are what the cache measurement in Prompt 6 reads.** Additional
sub-fields observed: `output_tokens_details.thinking_tokens`, `cache_creation` (with
`ephemeral_1h_input_tokens` and `ephemeral_5m_input_tokens`), `iterations[]` (per-API-
call breakdown), `service_tier`, `inference_geo`, `server_tool_use`, `speed`.

**Correction:** A `version` field IS present on all user and assistant records
(observed value: `"2.1.278"`). The earlier claim that no version field exists was wrong.

Reference parsers worth reading:
`kylesnowschwartz/agent-ouija` (Go) has a schema-drift allowlist test that fails the
build when unknown keys appear — copy that discipline.

---

## §5 — Prompt cache behaviour

`status: verified` (injection point list) / `status: community` (issue-tracker items) —
sources: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
plus anthropics/claude-code issues #83913, #27048, #29963.

- Any change early in the prompt (CLAUDE.md edits, settings, tool definitions) forces
  a cache warm-up from that point.
- `status: community` — issue #83913: `PreToolUse`/`PostToolUse` `additionalContext`
  gets re-serialised between turns during history rebuild, invalidating the cache even
  when the content is logically unchanged. **This is why R6 exists.**
- `status: community` — issue #29963: a `UserPromptSubmit` hook emitting a differing
  success/error string on a very long conversation caused roughly $45 of recomputation.
- `status: verify` — Claude Code appends plugin skills/commands/hooks after the
  conversation so they don't invalidate the existing prefix; enabling or disabling a
  plugin mid-session can still trigger a full rewrite (#27048).

**Design consequence:** SessionStart injection = one cache write per session, cheap.
Per-turn injection = repeated invalidation unless the text is byte-stable. Hence R4/R5
and `gates/gate_cache_safety.py`.

---

## §6 — Native memory surfaces ccmem must coexist with

`status: verified` — source: https://code.claude.com/docs/en/memory

**CLAUDE.md hierarchy**, broadest → most specific, all concatenated:
managed policy → `~/.claude/CLAUDE.md` → `./CLAUDE.md` or `./.claude/CLAUDE.md` →
`./CLAUDE.local.md`. Subdirectory CLAUDE.md files load on demand when files in those
directories are read.

`@path/to/file` imports resolve relative to the importing file. Max recursion depth is
4 hops — `status: verified` (docs: "with a maximum depth of four hops"). **Imports do
not save tokens** — imported files expand inline at launch. `.claude/rules/*.md` load
alongside CLAUDE.md, and `paths:` frontmatter scopes a rule to globs so it only loads
when matching files are touched — that *does* reduce what loads. Both confirmed.

**Auto memory** — `status: verified`, default-on (confirmed; specific version when it
became default not in official docs, so v2.1.59 figure is `status: unconfirmed`).
Storage: `~/.claude/projects/<project>/memory/` — a directory containing `MEMORY.md`
(the index) plus one topic file per memory (e.g., `user_role.md`, `feedback_testing.md`).
Only the first 200 lines of `MEMORY.md` (or 25KB, whichever comes first) loads at
session start. Topic files are **not loaded at startup**; Claude reads them on demand
using file tools. Disable with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.

**Observed (2026-09-22, v2.1.278):** `~/.claude/projects/c--Users-<user>-
OneDrive---<org>-Documents-ccmem/memory/` exists but is empty — no `MEMORY.md`
has been written yet for this project. Auto memory is enabled but has not yet recorded
any notes for the ccmem project.

> **This is Q5 in the brief.** Auto memory and ccmem both write durable context.
> Decide the relationship before building, or the same fact gets injected twice.

**Per-subagent memory** — `status: verify`. A subagent's YAML frontmatter can enable
auto memory (via a `memory` field); its auto memory directory is siloed from the
orchestrator's and from every other subagent's. The main conversation's auto memory is
not loaded into subagents; forks inherit it. Specific frontmatter values (`user|project|local`)
are `status: unconfirmed` — not shown in the fetched docs.

**Compaction** — project-root CLAUDE.md is re-read from disk and survives compaction;
conversation-only instructions do not.

---

## §7 — The API memory tool is NOT a Claude Code CLI feature

`status: verified` — source: https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool

`memory_20250818` plus context editing (`clear_tool_uses_20250919`) are **Claude
Developer Platform / Agent SDK** features. The model gets a file-shaped contract at
`/memories` with `view`/`create`/`str_replace`/`insert`/`delete`/`rename`, and *you*
implement the backend.

Anthropic's reported benchmark: context editing alone gave a 29% improvement over
baseline; memory tool plus context editing gave 39%; token consumption in a 100-turn
search evaluation dropped 84%.

**Consequence for ccmem:** you cannot just "turn on" the memory tool inside the
`claude` CLI. Inside the CLI, the available levers are hooks, files, and MCP. The
memory tool matters only if ccmem later grows an SDK-based harness.

One transferable detail: memory content is stored **verbatim** — `str_replace` is a
byte operation, so any normalisation or tidying of stored text corrupts it.

---

## §8 — Things known to break

`status: community` — from project issue trackers.

- A **missing hook script file** makes `UserPromptSubmit` fail at the OS level and can
  block all input until settings.json is hand-edited (claude-code #64223). Always
  wrap. See R1.
- **Windows** `printf`/pipe-close bugs have broken `UserPromptSubmit` hooks
  (claude-mem #2604). Avoid shell pipelines in hook commands; call the interpreter
  directly.
- **HTTP hooks to localhost** intermittently `ECONNREFUSED` (#29963).
- **Embedding dimension mismatches** across upgrades silently corrupt retrieval
  (claude-self-reflect history). Store the dim in the schema.

---

## §8b — `claude -p` does not fire UserPromptSubmit hooks

`status: verified` — empirically confirmed, 2026-09-22, v2.1.278

`claude -p '<prompt>'` (print/non-interactive mode) does **not** trigger UserPromptSubmit
hooks. Three prompts were run with a UserPromptSubmit hook registered in a `--settings`
override; none produced a payload. The hook output file was never written in any case.

**Consequence for testing:** The `!mem:` sigil cannot be confirmed via `claude -p`
automation. Gate tests for the capture path must invoke the hook script directly with a
synthesized payload (as `gate_hook_contract.py` already does), not via `claude -p`.

The sigil `!` has no documented special meaning in Claude Code prompts (unlike `#`, which
triggers the CLAUDE.md add-to-memory shortcut). The `prompt` field in a live UserPromptSubmit
payload is expected to carry the literal typed string — but this has not been empirically
confirmed because of the `-p` limitation above. `status: unconfirmed` — needs an
interactive session test to verify sigil preservation.

---

## §9 — Prior art worth reading before designing

`status: community`

| Project | Take this | Leave this |
|---|---|---|
| claude-mem | 3-layer retrieval (index → filter → fetch) for token savings; sub-10ms enqueue with async worker | API summarisation cost; heavy runtime |
| KoretyAutomate/claude-memory | Explicit hybrid scoring with recency half-life, frequency, and pinning; `status='archived'` supersession | ChromaDB dependency |
| claude-self-reflect | Decay half-life; importing existing `~/.claude/projects` transcripts as a cold-start corpus | Per-project collections causing scope confusion |
| Basic Memory | Human-editable markdown as the source of truth; entities/observations/relations from frontmatter + wikilinks | Model-invoked-only (MCP) retrieval |
| fortytwo/memory, memento | Minimal SQLite + FTS5 + sqlite-vec reference implementations | — |
| Hindsight | Scope as a tag (user/agent/session/org) rather than separate stores | Hosted backend |
| Cline Memory Bank | Zero-infrastructure markdown structure; good Phase 0 | Doesn't scale past a few hundred notes |
| Official MCP memory server | Entity/relation/observation JSONL format is a clean, inspectable model | No semantic search; model-invoked |

---

## §11 — Python hook startup cost on Windows / OneDrive

`status: verified` — measured 2026-09-23, v2.1.278, Windows 11 Enterprise, Python 3.14.3,
project directory on OneDrive - <org> (network-backed, sync filter driver active).

**Method:** `subprocess.run([sys.executable, '-c', 'pass'], capture_output=True)` x20 from
a running Python process — the same spawn path gates use. Three states measured:

| State | Min | p50 | p90 | Max |
|---|---|---|---|---|
| Quiet session (no recent commits) | 403ms | 534ms | — | 618ms |
| Active OneDrive sync (post-commit) | 1343ms | 1707ms | 1930ms | 2640ms |
| Full gate run under load (many concurrent spawns) | — | ~2500ms | — | 4600ms |

**Root-cause finding (2026-09-23):** The floor elevation during active sync is
**system-wide, not limited to OneDrive file reads**. Controlled comparison:
- `python -c pass` (no script file): p50=1730ms, max=2640ms during sync
- Script in `%LOCALAPPDATA%` (outside OneDrive): p50=2526ms, max=4358ms — equal or worse
- Hook in OneDrive repo: p50=1740ms, max=2488ms

Scripts in `%LOCALAPPDATA%` show similar or higher variance. Moving hook scripts
outside the sync boundary does NOT fix the problem. Likely cause: Windows Defender
scanning newly committed Python files, creating system-wide process-creation overhead
on every `subprocess.run` regardless of where the script lives.

**Stop hook own work** (hook logic separate from startup): ~55ms. Measured with
`CCMEM_THRESHOLD=0` and a live DB (capture + enqueue path). Module imports + JSON
parse + DB connect + insert + commit = ~55ms. Interpreter startup is ~10× the logic cost
in the quiet state; during active sync, startup dominates even more (1707ms vs 55ms).

**Gate thresholds** (`gates/config.json`):
- `interpreter_floor_ms`: 1700 (active sync p50 — when the gate is most likely run)
- `kill_switch_headroom_ms`: 800 (imports + load amplification during full gate runs)
- `budget_ms`: raised to p90 syncing-state totals; see config.json `_comment` field

**Phase 4 shim analysis:** Moving scripts outside the sync boundary is NOT the fix
(overhead is system-wide). A pre-warmed Python daemon avoids subprocess creation per
turn entirely — IPC to a resident daemon costs ~5ms vs 1700ms+ spawn. The daemon itself
can live in `%LOCALAPPDATA%\ccmem\` (outside OneDrive) to minimize its own startup cost,
but the per-turn saving comes from avoiding spawns, not from file location. A native
binary shim would also help startup for the daemon launch but would still be scanned by
Defender and not avoid the per-turn spawn cost without the daemon pattern.
See `docs/PLAN.md` Phase 4 section for the decision gate (depends on PHASE1-NOTES.md).

---

## §10 — Research grounding for the small-K decision

`status: community`

- "Lost in the Middle" (Liu et al., TACL 2024): U-shaped attention curve; accuracy on
  retrieval-augmented QA can fall from ~70–75% to ~55–60% when the relevant document
  sits in the middle of ~20 retrieved documents.
- Chroma's "Context Rot" report (2025): recall degrades as input length grows across
  18 frontier models tested.
- Anthropic's "Effective context engineering for AI agents": treat context as a finite
  attention budget; find the smallest set of high-signal tokens that gets the outcome.

**Consequence:** S7's low default K is deliberate. Raising it is a regression until an
eval says otherwise.
