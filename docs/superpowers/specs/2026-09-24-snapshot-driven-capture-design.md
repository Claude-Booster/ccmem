# Snapshot-Driven Capture (Option B) — Design

**Date:** 2026-09-24
**Status:** Draft for review (revision 2 — 3-hook shape)
**Supersedes capture model in:** DESIGN.md §Implementation (Stop-triggered per-turn
capture; UserPromptSubmit per-turn retrieval)

## Problem

Every hook invocation on this machine spawns a Python interpreter that Windows
Defender scans on startup. Two hooks fire per turn — `Stop` (capture) after the
assistant finishes, and `UserPromptSubmit` (sigil + opt-in retrieval) before it —
so each turn pays that spawn cost twice.

**The Defender exclusion was never a real lever.** Measured across three sessions:

| State | Production Stop-path p50 |
|---|---|
| No exclusion (2026-09-23, earlier) | 5154ms |
| Lib exclusion applied (2026-09-23) | 3056ms |
| Exclusion reverted (2026-09-24) | 2598ms |

Today's *exclusion-gone* p50 (2598ms) is **lower** than yesterday's
*exclusion-applied* p50 (3056ms); the floor moved the opposite direction over the
same period (1243ms → ~2300ms). The Defender exclusion's ~430ms effect sits inside
±1000–2000ms of run-to-run system/OneDrive-sync variance. We were reading signal
into noise for two days. The exclusion revert is confirmed (via
`Get-MpPreference`, not inferred), but the "back to 5s" magnitude is not supported
by observation — it is ~2.6s today, swinging 1.5–3.8s run to run.

Two conclusions follow. First, any latency claim in this project must rest on a
distribution, not a single-session p50. Second, and decisive here: the per-turn
spawn cost is multi-second, highly variable, and **not reliably reducible** —
exclusions revert (policy) *and* their effect is within the noise (measurement).
The only robust fix is to stop spawning per turn.

## Intended outcome

Eliminate all per-turn spawns while preserving turn-level capture granularity and
the durable `!mem:` sigil, losing no candidates to crashes, without a daemon and
without depending on IT policy.

Success criteria:
- No `Stop` and no `UserPromptSubmit` hook. ccmem fires only at SessionStart,
  PreCompact, SessionEnd. Zero per-turn spawns.
- The candidates enqueued over a session equal what the per-turn model would have
  enqueued (same `score_turn`, same threshold, same turn-pairs).
- `!mem:` writes still land in `memories` — durable, redacted, superseding.
- A refusal (sigil containing a secret) reaches the user without the user thinking
  to check.
- A session killed before SessionEnd loses no candidates permanently: the next
  session for that project sweeps them up, within a bounded time/byte budget.
- All acceptance gates stay green; gate changes are additive or remove checks for
  genuinely-removed features (never weakened to hide a failure).

## Constraints (CLAUDE.md rules)

- **R1** — hooks exit 0 on any error. All three surviving hooks keep the wrapper.
- **R2** — one SQLite file, no daemon. No process added.
- **R5** — per-turn injection opt-in / cache hazard. **Becomes moot**: the only
  per-turn injection path (`UserPromptSubmit` `additionalContext`) is deleted.
  DESIGN.md marks R5 as retired, not violated.
- **R7** — store decisions, not transcripts. Candidates remain scored turn-pairs
  pending review; only the trigger changes.
- **R8** — redact on write. See the R8 exposure section — the policy is unchanged
  but the exposure grows, and that is stated honestly, not glossed.

## Architecture

### Trigger model (3 hooks)

| Event | Hook | Role |
|---|---|---|
| `SessionStart` | `mem_inject.py` | Bounded recovery sweep first; then build and print the injection block once, including any refusals the sweep just discovered |
| `PreCompact` | `mem_snapshot.py` | Batch-capture new turns, mark captured rows `is_pre_compact=1` |
| `SessionEnd` | `mem_flush.py` | Batch-capture new turns, WAL checkpoint |
| `Stop` | — | Removed |
| `UserPromptSubmit` | — | Removed |

After removal, `mem_inject.py` (SessionStart) is the **only** emitter of
`additionalContext` — verified 2026-09-24. Nothing in the design assumes a hook
fires before Claude responds; the sigil's durable write and refusal feedback are
re-homed (below), not lost.

### Shared capture routine

`ccmem/capture.py`:

```
capture_transcript(con, transcript_path, session_id) -> CaptureResult
```

`CaptureResult` carries counts (candidates enqueued, sigil memories written,
refusals recorded) so callers and tests can assert outcomes.

Per turn-pair reconstructed from the transcript (one `user` record + the following
`assistant` `text` blocks concatenated across `apiBlockIndex`, up to the next
`user` record):

1. If the user text is a `!mem:` sigil (via `extract_sigil`): route to the
   **sigil path** (durable memory), not to candidates.
2. Otherwise: `score_turn`; if `>= threshold`, route to the **candidate path**.

**Sigil path** (durable-write semantics preserved exactly — asserted in tests):
- `redact(text)`. If redaction changed the text → **do not store**; record a row in
  `sigil_refusals` (redacted excerpt) and continue.
- Else `INSERT` into `memories` with `type='preference'`, `status='active'`,
  `scope` from the sigil, `content_hash` set; run `maybe_supersede`. Idempotent via
  `content_hash` (re-processing the same sigil record is a no-op).

**Candidate path**:
- `content_hash = sha256(FULL user_turn \x00 FULL assistant_turn)`. **Hash the full
  text; store the truncated text.** Hashing after truncation is a silent correctness
  bug: two long turns sharing their first `max_candidate_chars` would collide and
  `INSERT OR IGNORE` would drop the second. Hash and storage are separate concerns
  (change #1).
- Then truncate `user_turn`/`assistant_turn` to `max_candidate_chars` (config) for
  storage — enough for review, not the full turn. Bounds raw text at rest.
- `enqueue_candidate` with the full-text `content_hash` and the truncated columns.

**Candidate lifecycle and dedup permanence** (change #1): candidate rows are
**never hard-deleted**. Review sets `status` (`accepted`/`rejected`); the row stays
(confirmed in current `cmd_review`). Combined with the `content_hash` UNIQUE index,
this means a given turn-pair enqueues **at most once, ever** — rejected content is
never re-suggested, promoted content is never re-enqueued. This is a deliberate
invariant the dedup relies on: nothing may hard-delete candidate rows without
providing another permanent record of their hashes.

### Correctness vs efficiency (idempotency)

Two independent mechanisms, deliberately layered:

- **Correctness — `content_hash` UNIQUE (candidates) / `content_hash` (memories).**
  `candidates` gains a `content_hash` column with a UNIQUE index; `enqueue_candidate`
  becomes `INSERT OR IGNORE` on it (today's `INSERT OR IGNORE` on a fresh-uuid PK
  dedups nothing — this fixes that). This guarantees no duplicate candidate ever,
  regardless of session_id changes, resume, or fork. It is the correctness floor.
- **Efficiency — per-transcript high-water mark.** New table
  `transcript_progress(transcript_path PK, last_prompt_id TEXT, last_ordinal INTEGER,
  session_id TEXT, updated_at TEXT)`. Lets a scan skip already-processed turns
  instead of re-scoring the whole file. If the mark is wrong or stale, correctness
  is unaffected (the hash still dedups); only work is wasted.

**Why key the HWM on `transcript_path`, not `session_id`** (change #3): FACTS §4
says resume *appends to the same file*, so the path is stable across resume — the
mark continues correctly. A fork gets a new file (new session-uuid in the name), so
it starts a fresh mark; if a fork *copies* pre-fork turns into the new file, those
turns re-score under the new path but the `content_hash` dedups them. This makes
correctness independent of whether `session_id` changes on resume/fork — a property
FACTS §4 leaves `community`/unverified. Empirical confirmation of resume/fork
`session_id` behavior is still worth doing during implementation, but the design no
longer *depends* on it.

**Path normalization is mandatory** (change #4): a `TEXT PRIMARY KEY` is
case-sensitive, but Windows paths are not, and this machine also has 8.3 short paths
live — so `C:\Users\...`, `c:\users\...`, and `C:\Users\FREDER~1.MAN\...` are three
spellings of one file that would otherwise create three `transcript_progress` rows
with three independent marks (the 2nd and 3rd re-scoring everything). Every
`transcript_path` is normalized before use and stored normalized:
`os.path.normcase(os.path.realpath(path))` — `realpath` resolves 8.3 short paths and
symlinks to the canonical long form, `normcase` casefolds on Windows (no-op on
POSIX). The disabled-marker hash (kill switch) uses the same normalized form. A
fixture with three spellings of one path asserts a single `transcript_progress` row.

### promptId as a single point of failure (change #1)

The HWM position uses `prompt_id` (`promptId` on user records, FACTS §4 `community`
on a drifting schema). Mitigations, layered so drift degrades rather than stops:

- **Composite position.** Track both `last_prompt_id` and `last_ordinal` (count of
  user records processed). Use `prompt_id` when present; fall back to `last_ordinal`
  when a record lacks it. Capture keeps working if `promptId` is renamed/dropped.
- **Correctness unaffected.** Even if the HWM degrades, `content_hash` prevents
  duplicates — the worst case is re-scoring, not double-writing or data loss.
- **Loud on total drift.** If a transcript has user records but **none** carry
  `promptId`, capture emits a `WARN` and doctor reports a **hard failure** with a
  clear message ("transcript schema drift: no promptId on any user record —
  capture is running on ordinal fallback"). A fixture with `promptId` absent
  asserts this loud path (not a silent zero).

### SessionStart ordering (change #3)

The sweep runs **before** the injection block is built and printed — not after.
Reason: a refusal from a crashed session is only *discovered* by the sweep, so if
injection prints first, that refusal cannot surface until the session after next —
exactly the case (secret typed into `!mem:`, session died) where the user most needs
prompt feedback. Order:

1. Bounded recovery sweep (below). This may write candidates, sigil memories, and
   `sigil_refusals` rows.
2. Build the injection block against the now-updated store, including a refusal
   notice if the sweep recorded any.
3. Print once.

This puts the sweep on the critical path before injection. That is deliberate: a
slower-but-correct SessionStart beats fast-but-wrong feedback. The sweep stays
bounded (below) so SessionStart stays within its 10s timeout.

### Crash recovery (SessionStart, bounded — change #2)

Recovery rides the existing SessionStart spawn (no new process):

1. Resolve the project transcript dir (`~/.claude/projects/<munged-cwd>/`).
2. Candidate files = transcripts that are all of:
   - mtime newer than their `transcript_progress.updated_at` (dirty check;
     stat-only, no read, for already-current files);
   - mtime **after `initialized_at`** — the timestamp ccmem first ran `migrate`,
     stored in `schema_meta`. Transcripts predating ccmem's install are never swept
     (change #2). Recorded at first migrate.
   - **not marked disabled** (kill-switch exclusion, below).
   Ordered most-recent first.
3. Sweep with **two budgets**, not a file count:
   - `recovery_max_bytes` — cumulative bytes read across the sweep.
   - `recovery_max_ms` — wall-clock deadline; the sweep stops mid-file-list when
     exceeded and leaves the rest for next time.
4. Partial sweeps are safe: HWM records progress, `content_hash` dedups. Whatever is
   not swept this session is swept next session.

Both budgets live in `gates/config.json`. A gate asserts SessionStart stays within
its budget with a deliberately oversized transcript fixture present (change #2).

Residual gap (accepted, documented): a crashed transcript never touched again, or
consistently beyond the byte/time budget, is not recovered. Candidates are
low-stakes pending-review items.

### The kill switch must not be defeated by the sweep (change #2)

`CCMEM_DISABLED` means "ccmem does nothing" — including *retroactively*. Without
this, a session run with the kill switch on still writes a transcript, and the next
session's sweep captures from it: "does nothing" silently becomes "does it later,"
which is worse than no kill switch because the user believed it. The kill switch is
what the user reaches for precisely when doing something they don't want captured.

Mechanism (marker file, not a sweep-time flag check): `CCMEM_DISABLED` is a
launch-time env var, so a session is disabled for its whole lifetime and SessionStart
reliably sees it. When `mem_inject.py` runs with `CCMEM_DISABLED` set, before
short-circuiting it writes an empty marker at
`CCMEM_HOME/disabled/<sha256(normalized-transcript-path)>`. This is a tombstone, not
capture — no DB mutation, no scoring — and it is the one action a disabled session
takes, so the guarantee ("this transcript is never swept") holds. The sweep skips any
transcript whose normalized path has a marker.

A fixture asserts a transcript from a disabled session is never swept.

### Sigil refusal home (change — user decision)

**Both**, because they are not alternatives — they compose:
- The refusal is recorded in a `sigil_refusals` table. This persistence is what lets
  the message survive to the next session at all.
- SessionStart injection surfaces it: "N `!mem:` write(s) were refused last session
  (contained secrets) — run `ccmem list --refused`." This is what meets the
  requirement that a refusal reaches the user *without thinking to check* — it lands
  in context unprompted.
- Doctor reports the same table for free.

A synchronous per-turn warning is impossible without a per-turn hook; SessionStart
delivery is the earliest guaranteed unprompted surface once UPS is gone.

### R8 — exposure grows even though policy does not (change #4)

Stated plainly, not as "matches existing behavior": the `Stop` path stored one
turn-pair from `last_assistant_message`. `capture_transcript` parses **whole
transcripts**, and the recovery sweep reads **other sessions' transcript files**.
The redaction *policy* is unchanged (redact sigils on write to `memories`;
candidates hold raw scored text, as today), but the *volume and scope of raw text
at rest both grow*. Three consequences in this design:

- **Cap raw text per candidate.** Truncate `user_turn`/`assistant_turn` to
  `max_candidate_chars` (config). `score_turn` pattern-matches; review needs a
  readable excerpt, not thousands of tokens of transcript.
- **Extend `gate_secret_hygiene` to audit `candidates`** (`user_turn`,
  `assistant_turn`), not just `memories`. If secrets can sit there, the gate must
  see them. (It currently scans `memories` only — confirmed.)
- Candidate-stage redaction remains a pre-existing open question; this design does
  not change the policy, but it does bound and audit the exposure it enlarges.

### `is_pre_compact` (minor — user question)

Currently **write-only**: set by PreCompact, read by nothing. Its intended consumer
(prioritising compaction-surviving candidates during review) is unbuilt. This
design keeps setting it (PreCompact marks the rows it captured) and documents it as
**reserved** for review prioritisation. No consumer is invented now (YAGNI).

## Removals and config changes

- `settings.json`: remove ccmem `Stop` and `UserPromptSubmit` registrations (user
  config; the ccgate hooks are untouched).
- Delete `hooks/mem_capture.py` and `hooks/mem_retrieve.py`. Update
  `DESIGN_HOOK_NAMES` in `gate_scaffold.py` and the hook list in `DESIGN.md` in the
  same commit (gate comment sanctions this).
- `gates/config.json`:
  - Remove `hooks.Stop`, `hooks.UserPromptSubmit`, and their `budget_ms` entries.
  - Reframe `daemon_trigger` data-loss section: there is no Stop/UPS ratio. The new
    failure mode is "a transcript has turns past its `transcript_progress` mark that
    were never recovered" — doctor can report it. Latency trigger unchanged.
  - Add `recovery_budget` (`max_bytes`, `max_ms`) and `max_candidate_chars`.
- `gates/gate_cache_safety.py`: remove `check_per_turn_default_off` and the UPS
  byte-stability checks (feature gone). Update the SessionStart cross-session
  determinism check per change #5 (steady-state determinism + at-most-once
  sweep-induced change) — **in its own commit with the reasoning**, not silently.
- `gates/gate_hook_contract.py`: remove the "UserPromptSubmit never exits 2" case;
  contract now covers the three surviving hooks.
- `gates/gate_secret_hygiene.py`: also scan `candidates`.
- `gates/gate_schema_contract.py`: add `transcript_progress`, `sigil_refusals` to
  `EXPECTED_TABLES`; add `content_hash` to the `candidates` column list.
- `ccmem/cli.py` (doctor): reframe drop-rate → unrecovered-transcript detection;
  add revert-detection (below); report `sigil_refusals`; hard-fail on promptId
  total drift.
- Delete `tests/test_hook_mem_retrieve.py`; drop `mem_retrieve`/`mem_capture` from
  the `test_hook_safety.py` sweep.
- `docs/DESIGN.md`: 3-hook snapshot-driven model; R5 marked retired.

### Direct revert detection (change #5)

Doctor compares the current Defender exclusion list against the last confirmed
state and reports when an entry disappears. It reads the list via `Get-MpPreference`
(readable **non-admin** — confirmed 2026-09-24; the raw HKLM key is not, and this
account is non-admin). Output states its scope, so it never implies a latency cause:

```
Defender exclusion no longer present (reverted since 2026-09-23).
Note: measured latency impact is within run-to-run variance. This is
policy-revert insurance on a managed machine, not a performance signal.
```

### Cache determinism vs the sweep (change #5)

The sweep now writes to `memories` (sigil path) and `candidates` inside SessionStart.
The cross-session determinism gate asserts SessionStart output is byte-stable across
two runs against an unchanged store — but run 1's sweep changes the store, so run 2
legitimately differs. The gate is not deleted; it is updated deliberately, **in its
own commit with the reasoning**, to assert two things instead of one:
- **Steady state:** with no dirty transcripts present, two SessionStart runs are
  byte-identical (the injection block is stable when there is nothing to sweep).
- **At-most-once change:** a dirty transcript induces a sweep-driven change on the
  first run only; a second run with no new dirt is byte-identical to the first's
  post-sweep output.

Real-world consequence, written down rather than discovered: **the first session
after a crash burns one cache write** (the sweep mutates the store before injection,
so that session's prompt prefix differs from the prior warm one). This is acceptable
and rare — crashes are rare, and R4 already spends one cache write per session at
SessionStart — but it is a real cost of sweep-before-inject (change #3) and is
recorded here.

### Concurrency (change #6)

Two Claude Code sessions in the same project both fire SessionStart and both sweep.
WAL handles concurrent writers and `content_hash` dedups any overlap, but two things
must be explicit:
- **Partial final line.** A sweep may read a transcript that is being appended to
  right now, so the last JSONL line can be a truncated/partial JSON write. Parsing
  must **skip a malformed line and continue the file**, never abort the file on it.
  (A mid-file malformed line is different — that suggests real corruption — but the
  robust choice is skip-and-continue for all parse failures, matching the existing
  drift-tolerant-parser posture in FACTS §4.) A fixture with a truncated final line
  asserts the rest of the file is still captured.
- **Locked DB is now routine, not a rare R1 edge.** Concurrent sweeps will hit
  `database is locked`. Capture uses a short busy-timeout/retry and, on continued
  failure, exits 0 having done nothing (R1) — the HWM/`content_hash` make the next
  session's sweep pick up whatever this one dropped. No data loss, just deferral.

## Testing (fixture-driven, before implementation)

Fixtures live in `fixtures/` (already present; add synthetic transcript JSONL).

- `capture_transcript` salience: mixed salient/non-salient pairs → only salient
  enqueue, expected scores.
- Idempotency: run twice → second run enqueues 0; HWM at last `prompt_id`.
- Cross-event idempotency: PreCompact then SessionEnd on the same transcript → no
  duplicate candidates (asserts `content_hash` path).
- **Hash on full text (change #1):** two turn-pairs with identical first
  `max_candidate_chars` and divergent tails → **both** enqueue (guards the
  truncate-then-hash bug).
- **Rejected content does not re-enqueue (change #1):** reject a candidate, re-run
  capture over the same transcript → no new row (row-retention + hash permanence).
- Resume + fork (change #3): same-file resume continues the mark; forked file with
  copied pre-fork turns produces **no duplicates** (content_hash).
- **Path normalization (change #4):** three spellings of one transcript path
  (`C:\`, `c:\`, 8.3 short) → a single `transcript_progress` row.
- promptId totally absent (change #1): capture continues on ordinal fallback **and**
  emits WARN; doctor reports hard failure.
- **promptId mixed (change #7):** a transcript where some user records carry
  `promptId` and some don't → positioning switches between prompt_id and ordinal
  with **no skipped and no re-processed** turns across a PreCompact→SessionEnd
  sequence.
- **Kill switch (change #2):** a transcript from a `CCMEM_DISABLED` session (marker
  present) is **never swept**.
- **Pre-install bound (change #2):** a transcript with mtime before `initialized_at`
  is never swept.
- **Partial final line (change #6):** a transcript whose last line is truncated JSON
  → the malformed line is skipped and every complete prior turn is captured.
- Sigil durable write (assert exactly): `!mem:` record → one `memories` row,
  `type='preference'`, `status='active'`, redacted content, `maybe_supersede`
  applied; **not** a candidate.
- Sigil refusal: `!mem:` with a secret → no memory written, one `sigil_refusals`
  row; SessionStart surfaces the count; doctor reports it.
- Recovery crash: transcript with HWM left behind → SessionStart sweep captures
  exactly the missed pairs.
- Recovery clean: current transcript (mtime ≤ updated_at) → zero reads.
- Recovery budget (change #2): oversized transcript fixture → SessionStart stays
  within `recovery_max_ms`/`recovery_max_bytes`; unswept remainder captured next
  session (partial-safe).
- Text cap (change #4): long turn → stored `user_turn`/`assistant_turn` ≤
  `max_candidate_chars`.
- Secret hygiene (change #4): `gate_secret_hygiene` flags a planted secret in
  `candidates`.
- `is_pre_compact`: PreCompact marks the rows it captured.
- Turn pairing: assistant content across multiple `apiBlockIndex` records → one
  `assistant_turn`.
- Safety (R1): all three hooks exit 0 on malformed/empty stdin, missing DB, locked
  DB.

## Files touched

- Modify: `ccmem/capture.py` — `capture_transcript`, sigil routing, `content_hash`,
  text cap, promptId-drift WARN, ordinal fallback.
- Modify: `ccmem/db.py` — add `transcript_progress`, `sigil_refusals`; add
  `content_hash` (+ UNIQUE) to `candidates`; record `initialized_at` in `schema_meta`
  at first migrate (change #2 sweep lower-bound); bump `schema_version` 1→2; migrate.
  Add a short SQLite busy-timeout on connect (change #6, locked-DB routine).
- Modify: `hooks/mem_inject.py` — bounded recovery sweep + surface refusals after
  injection.
- Modify: `hooks/mem_snapshot.py` — capture then mark `is_pre_compact`.
- Modify: `hooks/mem_flush.py` — capture then checkpoint.
- Delete: `hooks/mem_capture.py`, `hooks/mem_retrieve.py`.
- Modify: `ccmem/cli.py` — doctor (drop-rate reframe, revert detection, refusals,
  promptId drift).
- Modify: `gates/gate_scaffold.py`, `gates/gate_schema_contract.py`,
  `gates/gate_cache_safety.py`, `gates/gate_hook_contract.py`,
  `gates/gate_secret_hygiene.py`, `gates/config.json`.
- Add: gate for SessionStart recovery budget (oversized fixture).
- Delete: `tests/test_hook_mem_retrieve.py`. Modify: `tests/test_hook_safety.py`.
- Add: `fixtures/` synthetic transcript JSONL (salient/non-salient, sigil,
  sigil+secret, resume, fork, promptId-absent, promptId-mixed, oversized,
  truncated-final-line, identical-prefix-divergent-tail).
- Modify: `docs/DESIGN.md`.

(Note: `plugin/hooks.json` does not exist yet — the `plugin/` layout in CLAUDE.md
is aspirational. When created it must reflect the 3-hook model. Not part of this
change.)
