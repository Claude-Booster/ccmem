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

### Two executables: Windows Store stub vs real Python

`python` on PATH resolves to `C:\...\WindowsApps\python.exe` — the **Windows Store app
stub**, not the real interpreter (`C:\...\AppData\Local\Python\pythoncore-3.14-64\python.exe`).
The stub re-launches the real interpreter, creating two OS-level processes per hook spawn.

**Critical calibration gap (discovered 2026-09-23):** `gates/_common.py` uses
`sys.executable` to run hooks, which within a running Python process is always the
real interpreter. Production hooks in `~/.claude/settings.json` use `"command": "python
\"hook.py\""` which goes through the Store stub. **The gates have been measuring the
real interpreter; production hooks pay Store stub overhead on top.**

### Measured costs (2026-09-23)

All measurements use `subprocess.run(cmd, input=b"{}", capture_output=True)` — the same
call path as the gates.

**Real Python (`sys.executable`) — what gates measure:**

| State | Min | p50 | p90 | Max |
|---|---|---|---|---|
| Quiet session (no recent commits) | 403ms | 534ms | — | 618ms |
| Active OneDrive sync (post-commit) | 1343ms | 1707ms | 1930ms | 2640ms |
| Full gate run under load (many concurrent spawns) | — | ~2500ms | — | 4600ms |

**Store stub vs real Python — what production hooks actually pay:**

Measured under elevated load (post-measurement-batch, system stressed); absolute values
are inflated ~6x but the ratio is the meaningful number:

| Executable | p50 | Max | Ratio vs real |
|---|---|---|---|
| Store stub (`python`) | 15,665ms | 17,479ms | **4.7× real** |
| Real Python (direct path) | 3,350ms | 3,630ms | 1× |
| Real Python with `-S` | 3,661ms | 4,070ms | ~1× (no meaningful difference) |

Quiet-state stub cost estimate: 534ms × ~2–3× (load-test ratio under calmer conditions) =
**~1,000–1,600ms per hook spawn** just for the Store stub overhead in quiet state.
During active sync this is compounded further. This estimate will be replaced with a
clean measurement once the system is freshly rebooted.

### Production path vs gate path

The gate uses `subprocess.run([real_py, script], ...)` — a direct list invocation that
creates one process. Production hooks in settings.json use a command string with spaces
(the "OneDrive - <org>" path), which requires `shell=True` and spawns `cmd.exe`
first. This adds a second process creation per hook call.

Measured during active sync (2026-09-23):

| Invocation | Exclusions | p50 | p90 | max |
|---|---|---|---|---|
| Gate [list, no shell] | none | 2636ms | 4360ms | 4360ms |
| Production [string, shell=True] | none | **4685ms** | **7705ms** | 7705ms |
| Gate [list, no shell] | Lib only | 1645ms | 1917ms | 2212ms |
| Production [string, shell=True] | Lib only | **3056ms** | **4085ms** | 4152ms |
| Floor [`python -c pass`] | Lib only | 1243ms | 1541ms | 2095ms |

**At p50=4685ms against a 5s Stop timeout, production hooks were at the edge without
exclusions.** Stop timeout raised to 10s. With Lib exclusion applied, production p50=3056ms
— well within 10s limit. The shell overhead is consistently **1.86×** the list-invocation
gate measurement across all states.

### Defender exclusion results — corrected finding

Earlier test used `-ExclusionProcess` (process scan). New test used `-ExclusionPath`
(file read scan). Measured during active sync (2026-09-23), 8 runs each:

| Exclusion state | p50 | p90 | Delta |
|---|---|---|---|
| Baseline (no exclusions) | 1796ms | 2494ms | — |
| + hooks dir path exclusion | 1565ms | 2159ms | -13% |
| + hooks dir + Python Lib path exclusion | **1132ms** | **1677ms** | **-37%** |

The Python Lib directory (`pythoncore-3.14-64\Lib\`) is the big contributor — Defender
scans each .py file imported at startup. Excluding it saves ~430ms on top of the hooks
dir exclusion. Combined path exclusions save 37%, vs only 9% for the process exclusion.

**With Lib-only exclusion in production (shell=True, active sync) — MEASURED 2026-09-23:**

| Metric | p50 | p90 | max |
|---|---|---|---|
| Production (shell=True, short path) | **3056ms** | **4085ms** | 4152ms |
| Gate (list, no shell) | 1645ms | 1917ms | 2212ms |
| Floor (python -c pass) | 1243ms | 1541ms | 2095ms |

Shell overhead: 3056/1645 = **1.86×** (consistent with prior 1.8× measurement).
Hook-file overhead (gate − floor): **402ms** — Defender still scans `mem_capture.py` on
read from OneDrive. hooks-dir exclusion would eliminate this; prior isolated measurement
saved ~231ms on gate path → estimated gate ~1414ms, production **~2825ms** with both
exclusions.

**With both path exclusions in production (shell=True, active sync) — estimated:**
~2825ms. Lib exclusion measured; hooks-dir exclusion deliberately skipped (see below).

**hooks-dir exclusion: decided against (2026-09-23).**
Saving: ~231ms off gate path → estimated production ~2825ms vs measured 3056ms.
Not applied because the hooks directory (`ccmem\hooks\`) is a corporate-synced path
containing live, frequently-edited code. A standing AV exception on actively-written
code is a permanent security trade-off, not a one-time tuning step. The 231ms saving
does not justify it. This decision should not be revisited unless production p50
climbs materially above 3056ms.

**Measured baseline (2026-09-23, Lib exclusion only):**
- Floor (`python -c pass`): p50=1243ms, p90=1541ms
- Gate (list, no shell): p50=1645ms, p90=1917ms
- Production (shell=True, short path): p50=3056ms, p90=4085ms

This baseline was valid only while the Lib exclusion was live. It was reverted within
~24h — see the next finding. Treat these numbers as transient, not the sustained state.

### Defender exclusion is NOT durable on this machine — reverted by Intune (2026-09-24)

**The Lib exclusion applied 2026-09-23 was gone by the next morning.** Confirmed by
dumping `(Get-MpPreference).ExclusionPath`: the `pythoncore-3.14-64\Lib` path is absent
from both the elevated (~270-entry, policy-merged) view and the non-admin (1-entry local)
view. Every entry in the machine's exclusion list is corporate/IT tooling (Nexthink,
Tanium, Mandiant, Forcepoint, dgagent, Genetec, AttackIQ, Citrix) — none user-added.

Root cause: `HKLM\SOFTWARE\Policies\Microsoft\Windows Defender\Exclusions` exists →
Intune/Group Policy actively manages Defender exclusions on this machine and wipes
user `Add-MpPreference` additions at each policy refresh (~8h cycle). `DisableLocalAdminMerge`
is not set, so the exclusion was *reverted*, not *ignored* — it worked for a few hours,
then the refresh removed it.

**Consequences:**
- User-applied Defender exclusions are not a viable permanent fix on this machine.
  Re-applying is whack-a-mole against the policy refresh.
- There is no stable "sustained state" number — see the variance finding below. The
  1243ms/3056ms figures are transient, and so is any single-session number.
- The `doctor` revert-detection (defender_state.json + timing comparison) caught this
  on day one — working as designed. But there is no durable "good" baseline to save,
  so `--save-baseline` should not be run here.
- The account is non-admin; UAC elevation switches to a separate admin account, so
  even the registry-read path in doctor cannot run as the user. Confirmed 2026-09-24.

**Fix direction:** the durable levers no longer include Defender exclusions. Remaining:
- **Option B (fire Stop capture less often):** move per-turn capture off the Stop hook
  (PreCompact + SessionEnd only). Removes the per-turn spawn cost entirely; no dependency
  on Defender or IT policy. Cheapest durable fix. See DESIGN.md.
- **Option C (pre-warmed daemon):** most complex; gated on measured drop rate.
- **Request IT to add the exclusion to the Intune policy:** slow, uncertain, not worth
  it for a personal dev tool.

### Defender exclusion effect is within measurement noise (2026-09-24)

The exclusion was never a real latency lever. Production Stop-path p50 across three
measurement sessions:

| State | Production p50 | Session floor |
|---|---|---|
| No exclusion (2026-09-23, earlier) | 5154ms | ~1707ms |
| Lib exclusion applied (2026-09-23) | 3056ms | 1243ms |
| Exclusion reverted (2026-09-24) | 2598ms | ~2300ms |

⚠️ **These are single-session p50s and must not be compared as if stable.** Today's
*exclusion-gone* p50 (2598ms) is **lower** than yesterday's *exclusion-applied* p50
(3056ms), and the floor moved the opposite way. The Defender exclusion's measured
effect (~430ms in the isolated 8-run test) sits **inside** ±1000–2000ms of
run-to-run system/OneDrive-sync variance. For two days we read signal into noise.

What survives:
- The **revert is confirmed** via `Get-MpPreference` (observation, not inference).
- The **"back to ~5s" magnitude is not supported** — production is ~2.6s today,
  swinging 1.5–3.8s run to run.
- The per-turn spawn is multi-second, variable, and **not reliably reducible** —
  exclusions revert (policy) *and* their effect is within noise (measurement). The
  only robust fix is to stop spawning per turn → snapshot-driven capture (Option B).

**Rule going forward:** any latency claim in this project must rest on a
distribution (min/p50/p90/max over ≥8 runs in one session), never a single-session
p50 compared against another session's single-session p50. The spawn cost is too
noisy for point comparisons.

### Findings on other hypotheses

- **`-S` flag (skip site imports):** No improvement (3.66s vs 3.35s — noise).
  `pywin32_bootstrap` (20ms) and total site startup (~58ms) are not the bottleneck.
- **PYTHONPATH:** Empty — not a factor.
- **Site-packages:** 17 entries only — not a factor.
- **Script location (OneDrive vs %LOCALAPPDATA%):** No effect on spawn cost. Ruled out.
- **Controlled Folder Access:** Audit mode (2), not blocking — not a factor.
- **Process exclusion:** -9% — the wrong exclusion type; file-path exclusions matter.

### hook_log and production observability

The `hook_log` table now records `duration_ms` (within-Python execution time, measured
with `time.monotonic()` at hook entry/exit — does NOT include OS process creation).
All five hooks write timing on their active path. Doctor displays duration alongside
event and excerpt. This gives a real distribution from real use, separate from the
synthetic gate measurements.

Note: No production DB existed as of 2026-09-23. All timing data to date is from
synthetic measurements. The daemon decision should incorporate PHASE1-NOTES.md data
once production sessions accumulate hook_log entries.

### Stop hook own work

~55ms (within-Python logic). Module imports + JSON parse + DB connect + insert + commit.
Startup dominates at all system states; logic cost is ~5% of the active-sync floor.

### Gate thresholds (`gates/config.json`)

- `interpreter_floor_ms`: 1700 (active-sync p50 for real Python, no shell)
- `kill_switch_headroom_ms`: 1500 (raised from 800; covers hooks-dir Defender scan ~400ms + stdlib imports ~200ms + concurrent test load ~400ms; total threshold = 3200ms)
- `budget_ms`: p90 syncing-state totals

**Gate vs production gap:** gates measure list invocation; production uses shell. After
applying both Defender path exclusions, production floor is ~2038ms — within the 6000ms
gate budget. Without exclusions it is 4685ms, which is also within budget but leaves
only 315ms to the Stop timeout (5000ms).

### Root cause summary

| Cause | Contribution | Status |
|---|---|---|
| Windows Store stub (`python` on PATH) | 2–5× overhead | **Fixed** — settings.json uses real Python path |
| `cmd.exe` shell (spaces in path) | ~1.8× overhead on top of real Python | Remaining — affects production only |
| Defender file-path scanning (Lib) | ~2098ms production (41%) during active sync | **Not durably fixable** — Lib exclusion reverts within ~24h (Intune-managed, see 2026-09-24 finding) |
| OneDrive sync filter driver / system I/O | ~600ms elevation (534ms→1132ms with exclusions) | Irreducible without eliminating spawns |
| Defender process exclusion | ~9% | Negligible — wrong exclusion type |
| Python site imports (`pywin32_bootstrap`) | ~20ms | Noise |
| Script location, PYTHONPATH, site-packages | No effect | Ruled out |
| Controlled Folder Access | No effect (audit mode) | Ruled out |

### Phase 4 analysis

**Current state (post Store-stub fix, no durable exclusion):** Defender exclusions revert
within ~24h on this Intune-managed machine (see 2026-09-24 finding), so the sustained state
is no-exclusion: production Stop hook ~5154ms p50, floor ~1707ms. The 10s Stop timeout
(raised from 5s) prevents data loss, but every turn still pays a multi-second pause.
Since the Defender lever is not durable, the remaining durable fix is architectural:
Option B (fire Stop capture less often — PreCompact/SessionEnd only) is the cheapest and
removes the per-turn spawn cost without depending on IT policy. Option C (daemon) if
Option B proves insufficient.

### Paused-OneDrive measurement (2026-09-23)

Sync paused from system tray; 12 runs each. Note: OneDrive was still actively syncing
when the script started and only fully paused mid-run for the production path test.

| Metric | Runs 1-9 | Runs 10-12 (truly paused) | p50 all |
|---|---|---|---|
| Gate path [list, no shell] | 5839-8046ms | — | **6580ms** |
| Production path [string, shell=True] | 10871-25327ms | 2712-3926ms | — |
| Interpreter floor (`-c pass`) | 1107-1751ms | consistent | **1333ms** |

**Key finding — paused ≠ quiet.** The filter driver overhead persists even with sync
paused. Interpreter floor paused (1333ms) ≈ active sync (1707ms). The sync activity
itself contributes only ~370ms; the driver interception is always present.

**Gate-path with CCMEM_DISABLED is anomalously high (6580ms ≈ active-sync 2636ms).** The
~5250ms gap above interpreter floor is Defender scanning `mem_capture.py` on read
from OneDrive. Defender exclusions (which we reverted after testing) eliminated this cost.

**Production path truly paused (runs 10-12): 2712-3926ms.** This is the OneDrive-paused
best-case for production hooks without Defender exclusions. It matches gate-path
active-sync (2636ms) closely — the production overhead (shell) ≈ the sync overhead.

**With Lib exclusion, active sync, production path — MEASURED 2026-09-23:**
p50=**3056ms**, p90=4085ms. Gate path: p50=1645ms. Floor: p50=1243ms.
Lib exclusion saved 41% off production p50 vs no-exclusion baseline (5154ms).

**With both exclusions (Lib + hooks-dir), active sync, production path — estimated:**
~2825ms. hooks-dir exclusion not yet applied; estimate based on prior isolated saving
of ~231ms on gate path.

**With both exclusions + paused sync:**
Paused-sync floor (1333ms) ≈ active-sync floor with Lib exclusion (1243ms) — they are
close. Paused sync does not materially improve on Lib exclusion. No separate measurement
taken; paused+both-exclusions production is expected within 200ms of active+both-exclusions.

**With exclusions + hooks installed outside OneDrive (proper plugin path):**
⚠️ **NOT MEASURED** — gate overhead above floor is 402ms with Lib exclusion (Defender
scans hook script on read from OneDrive). Moving hooks outside OneDrive would eliminate
this filter-driver interception; production estimate: floor(1243ms) × 1.86 ≈ ~2310ms.
Not confirmed.

**Short-path settings.json fix (2026-09-23):**
Hook commands updated from quoted long path to 8.3 short path (no spaces). Measured
effect on shell=True invocation: **none.** Old quoted path p50=5154ms; short path
p50=5253ms — within noise. Shell overhead (~1.7x vs list invocation) comes from cmd.exe
being spawned at all, not from path quoting. Fix is harmless but not a performance win.
Actual production hook timing depends on how Claude Code internally spawns hooks —
if it uses direct CreateProcess without cmd.exe, short paths may help; untested.

**Stop timeout raised to 10s** (2026-09-23 — from 5s). UserPromptSubmit also raised to
10s. Rationale: with p90=6933ms under active sync, the 5s limit was causing guaranteed
data loss on every high-sync turn. The 10s limit provides ~3s headroom at p90.

**Daemon trigger thresholds (set 2026-09-23, revised 2026-09-23):**
Stored in `gates/config.json → daemon_trigger`. Thresholds are now expressed as
measured Stop hook drop rate, not spawn milliseconds. See config.json for rationale.

The drop rate is measurable from hook_log: if Stop events are materially under-represented
relative to UserPromptSubmit events (which fire once per turn), missing entries represent
turns where Stop was killed. Doctor can compute this ratio.

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
