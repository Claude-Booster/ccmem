# Snapshot-Driven Capture (Option B) — Design

**Date:** 2026-09-24
**Status:** Draft for review
**Supersedes capture model in:** DESIGN.md §Implementation (Stop-triggered per-turn capture)

## Problem

On this Intune-managed Windows machine, every hook invocation spawns a Python
interpreter that Windows Defender scans on startup, costing ~5s during active
OneDrive sync. The `Stop` hook fires once per turn, so every turn pays that cost
after the assistant finishes. Defender path exclusions would cut it, but they are
reverted by corporate policy within ~24h (see FACTS.md §11, 2026-09-24 finding),
so they are not a durable fix.

The per-turn spawn cost is the problem. The two durable levers are (C) a
pre-warmed daemon that keeps per-turn spawns cheap, and (B) firing capture off
events that do not occur per-turn. C was rejected: it violates R2 (no daemon),
and it does not capture at finer granularity than B — both score the same
turn-pairs, because B reads the same transcript C would. C's only edge is
durability of unsaved candidates (per-turn commit), which is addressed here by
crash recovery. See the B-vs-C granularity analysis in the conversation of
2026-09-24.

## Intended outcome

Eliminate the per-turn `Stop` spawn while preserving turn-level capture
granularity and losing no candidates to crashes, without a daemon and without
depending on IT policy.

Success criteria:
- No `Stop` hook fires; no post-turn pause from ccmem capture.
- The set of candidates enqueued over a session equals the set the per-turn
  model would have enqueued (same `score_turn`, same threshold, same turn-pairs).
- A session killed hard before `SessionEnd` loses no candidates permanently: the
  next session for that project sweeps them up.
- All acceptance gates stay green without weakening any gate.

## Constraints (from CLAUDE.md rules)

- **R1** — hooks exit 0 on any error (malformed stdin, missing/locked DB, import
  failure). Unchanged; all rewritten hooks keep the exit-0 wrapper.
- **R2** — one SQLite file, no daemon. This design adds no process.
- **R7** — store decisions, not transcripts. Candidates are scored turn-pairs
  pending review, as today; only the trigger changes.
- **R8** — redact on write. Unchanged, and worth stating precisely: the current
  `Stop` path does **not** redact at capture. Candidates hold raw turn text;
  redaction runs at promotion (`cmd_add` → `redact`) when a candidate becomes a
  memory row. `capture_transcript` matches this exactly — no redaction at the
  candidate stage. (Whether candidates *themselves* should be redacted is a
  pre-existing question this design does not change or address.)

## Architecture

### Trigger model

Capture becomes transcript-driven, run at events that already fire without a
per-turn spawn:

| Event | Hook | Capture role |
|---|---|---|
| `PreCompact` | `mem_snapshot.py` | Batch-capture new turns, then mark them `is_pre_compact=1` |
| `SessionEnd` | `mem_flush.py` | Batch-capture new turns, then WAL checkpoint |
| `SessionStart` | `mem_inject.py` | Recovery sweep for turns a crashed prior session never captured |
| `Stop` | — | Removed |

All three capture paths call one shared routine, `capture_transcript`.
`score_turn`, the salience threshold (`CCMEM_THRESHOLD`, default 4), and
`enqueue_candidate` are unchanged.

### Shared capture routine

`ccmem/capture.py`:

```
capture_transcript(con, transcript_path, session_id) -> int
```

Behavior:
1. Read the high-water mark for `session_id` from `session_progress`.
2. Parse the transcript JSONL, reconstructing `(user_turn, assistant_turn)`
   pairs. A pair is one `user` record plus the concatenated `text` blocks of the
   `assistant` records that follow it, up to the next `user` record. Assistant
   content is split across records by `apiBlockIndex` (FACTS.md §4), so text
   blocks are gathered in order.
3. Skip pairs at or before `last_prompt_id` (the mark). Process only newer pairs.
4. For each new pair: `score_turn`; if `>= threshold`, `enqueue_candidate`.
5. Advance the mark to the last processed `prompt_id`; set `updated_at` to now.
6. Return the count enqueued.

Idempotent by construction: re-running over the same transcript processes no
pairs at or before the mark, so it enqueues nothing new. This is what makes it
safe to call at both `PreCompact` and `SessionEnd`, and repeatedly.

Turn identity uses `prompt_id` (present on `user` records as `promptId`,
FACTS.md §4) rather than byte/line offset, so it survives transcript rewrites and
partial reads. Pairs whose user record has no `promptId` (e.g., pre-first-input
system turns) are not scored.

### High-water mark

New table (additive — `gate_schema_contract` is a subset check, stays green;
the table is added to `EXPECTED_TABLES` to assert it exists):

```sql
CREATE TABLE IF NOT EXISTS session_progress (
    session_id    TEXT PRIMARY KEY,
    last_prompt_id TEXT,
    updated_at    TEXT NOT NULL
);
```

`schema_version` in `schema_meta` bumps 1 → 2; `migrate` adds the table
idempotently.

### Crash recovery (SessionStart)

Recovery rides the existing `SessionStart` spawn (`mem_inject.py`) — no new
process. After emitting the injection block, it runs a bounded sweep:

1. Resolve the project's transcript directory (`~/.claude/projects/<munged-cwd>/`,
   FACTS.md §4).
2. For each transcript file modified within a recency window (default 7 days),
   compare the file's mtime to the `updated_at` of that session's
   `session_progress` row.
   - mtime ≤ stored `updated_at` (or file already fully processed): skip —
     stat only, no read.
   - mtime > stored `updated_at`, or no row exists: the transcript has
     unprocessed turns → `capture_transcript` it.
3. Cap the number of files read per sweep (default 5, most-recent first) to bound
   worst-case latency on the injection path.

Normal case: every transcript is current, the sweep is stat-only, zero reads.
Crash case: the crashed transcript is "dirty" and is swept whether the user
resumes it or starts fresh in the same project.

Residual gap (accepted, documented): a crash whose transcript is never touched
again within the recency window, or beyond the per-sweep file cap, is not
recovered. Candidates are low-stakes pending-review items, so this is acceptable.

## Removals and config changes

- `settings.json`: remove the ccmem `Stop` registration (user config, not in
  repo). The ccgate `Stop` hook is untouched.
- `hooks/mem_capture.py`: removed. Its scoring/enqueue logic lives in
  `capture_transcript`. `DESIGN_HOOK_NAMES` in `gate_scaffold.py` and the hook
  list in `DESIGN.md` are updated in the same commit.
- `gates/config.json`:
  - Remove `hooks.Stop` and `budget_ms.Stop`.
  - `daemon_trigger` data-loss section (Stop/UPS drop rate) is reframed: with no
    Stop hook there is no Stop drop. The failure mode becomes "PreCompact and
    SessionEnd both failed to fire/complete for a session," detectable as a
    session with transcript turns past its `session_progress` mark that was never
    recovered. Doctor can report this.
  - Latency trigger (`spawn_warn_ms`, `spawn_daemon_ms`) is unchanged.
- `DESIGN.md`: capture model section updated to snapshot-driven; Option B marked
  as adopted.

## Open decision (not bundled here)

`UserPromptSubmit` (`mem_retrieve.py`) still spawns every turn, before Claude
responds. With per-turn injection off (R5 default), its only live job is catching
the `!mem:` sigil. Option B removes the post-turn `Stop` pause but not this
pre-turn pause. Options to address it later: gate the hook to no-op more cheaply,
or move sigil capture elsewhere. Deliberately out of scope for this design; flagged
so the per-turn-latency win is not overstated.

## Testing (fixture-driven, before implementation)

Fixtures live in `fixtures/`. New synthetic transcript JSONL fixtures are added
there.

- `capture_transcript` — salience: a transcript fixture with a mix of salient and
  non-salient turn-pairs; assert only salient ones enqueue, with expected scores.
- `capture_transcript` — idempotency: run twice; second run enqueues 0; HWM equals
  the last `prompt_id` after both.
- Cross-event idempotency: `PreCompact` then `SessionEnd` on the same transcript;
  assert no duplicate candidates.
- Recovery — crash: a transcript fixture with `session_progress` left behind;
  `SessionStart` sweep enqueues exactly the missed pairs and advances the HWM.
- Recovery — clean: a fully-processed transcript (mtime ≤ `updated_at`); sweep
  reads zero files.
- `is_pre_compact`: `PreCompact` marks the rows it captured with
  `is_pre_compact=1`.
- Safety (R1): all four hooks exit 0 on malformed stdin, empty stdin, missing DB,
  and locked DB.
- Turn pairing: assistant content split across multiple `apiBlockIndex` records is
  concatenated into one `assistant_turn`.

## Files touched

- Create: `ccmem/capture.py` — add `capture_transcript` (extend existing file).
- Create: `fixtures/` — synthetic transcript JSONL fixtures.
- Modify: `ccmem/db.py` — add `session_progress`, bump schema_version, migrate.
- Modify: `hooks/mem_snapshot.py` — capture then mark `is_pre_compact`.
- Modify: `hooks/mem_flush.py` — capture then checkpoint.
- Modify: `hooks/mem_inject.py` — add recovery sweep after injection.
- Delete: `hooks/mem_capture.py`.
- Modify: `gates/gate_scaffold.py` — update `DESIGN_HOOK_NAMES`.
- Modify: `gates/gate_schema_contract.py` — add `session_progress` to
  `EXPECTED_TABLES`.
- Modify: `gates/config.json` — remove Stop entries; reframe daemon data-loss
  trigger.
- Modify: `docs/DESIGN.md` — snapshot-driven capture model.
- Tests: `tests/` — the fixture-driven tests above.

(Note: `plugin/hooks.json` does not exist yet — the `plugin/` layout in CLAUDE.md
is aspirational. When the plugin manifest is created, it must reflect the 4-hook
model, not 5. Not part of this change.)
