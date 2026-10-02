# ccmem — postmortem

ccmem was a persistent-memory tool for Claude Code: capture decisions and
preferences during a session, store them in one local SQLite file, and inject
them back at the start of the next session so the assistant didn't start cold.
It was built over roughly three weeks, dogfooded, and retired. The commit-time
identifier/secret guard that lived alongside it was kept; everything that was
"memory" was removed. The full pre-retirement tree is recoverable at the git tag
`v0-memory-archive`.

This document is the actual output of the work. The code is gone; the reasons it
went the way it did are the thing worth keeping. Three findings, kept separate
because they have different causes and different defenses.

---

## Finding 1 (lead) — the drift pattern: a check and the thing it claimed to cover came apart, and nothing green could tell me

The recurring, most important failure was not a bug that broke a test. It was a
check and its subject silently *separating* — the test still passed, the config
still parsed, the CI still went green, but the green no longer meant what it
claimed. Every instance below was found by **reading**, not by running anything.
A passing suite was structurally incapable of surfacing them.

1. **`redact.py` passed its tests and was called by nothing.** The secret
   redactor had a full unit suite, all green. It was never wired into the commit
   guard (the guard uses gitleaks/TruffleHog), and once the memory store's write
   path was the only caller, removing the store left it an orphan. The tests
   exercised the function; nothing tested whether anything *called* it.

2. **The compaction test proved nothing because the control was missing.** An
   early claim held that `@import`ed memory survived context compaction, "proven"
   by a sentinel string reappearing across runs. Adding a control — a unique
   string placed only in filler, never in the import — showed the control *also*
   survived, which meant compaction had never actually fired in the test
   (repetitive filler tokenizes too densely to cross the threshold). The original
   test passed for a reason unrelated to what it claimed to measure. See
   FACTS.md §6, "Revised conclusion."

3. **The guard excluded its own source from its own scan.** The identifier guard
   skipped the entire `.githooks/` tree when scanning staged content, on the
   stated grounds that it "defines the labels." But the hook sources carry only
   opaque labels, not real patterns — so the exclusion protected nothing and
   blinded the guard to a restricted identifier sitting in its own comments.
   (It had in fact happened: a real identifier lived in a hook comment in commit
   `d0c9585` and reached the public repo only to be scrubbed by an unrelated PR —
   not caught by the guard.) Fixed by narrowing the exclusion to the two
   pattern-definition files.

4. **CI kept that exclusion after the local hook dropped it.** The server-side
   `verify.yml` had the identical `.githooks/*` blind spot. Fixing the local
   hook without fixing CI would have left the hole open on exactly the path a
   local `--no-verify` is supposed to backstop. Two copies of one rule drifted
   independently; fixing one is not fixing the rule.

5. **Five hooks registered in `settings.json`, pointing at scripts about to be
   deleted, on a machine where the application layer blocks user hooks so they
   never fired anyway.** `allowManagedHooksOnly:true` (corporate policy) silently
   ignores user-defined hooks, which is why injection had to move to `@import` in
   the first place. But the five `mem_*` hook registrations stayed in
   `~/.claude/settings.json` — dead config, invisible in both directions: the
   policy layer ignored them without complaint, and nothing in the repo knew they
   were still there. Two of the five (`mem_capture.py`, `mem_retrieve.py`) pointed
   at scripts that **never existed as files at all**. Had the code been deleted
   before this was found, every session start would have fired hooks at deleted
   paths. **The policy block is what made it invisible — a layer that silently
   ignores configuration is the same shape as a test that silently covers
   nothing.** Both report success while meaning nothing.

**Near-miss, recorded honestly:** the guard passes its *own* repository's secret
scan only because a `gitleaks` allowlist exempts its test fixtures (which contain
deliberately fake, secret-shaped strings). That allowlist is necessary — without
it the repo cannot commit its own tests — and it is also a hole: anything added
to those files is unscanned. Necessary and a weakness are not mutually exclusive.

**The defense this produced:** read the exclusion lists, not the pass/fail line.
A green check is evidence that the covered thing works, never evidence that the
thing you care about is covered. When a checker exempts part of its own domain,
that exemption is where the next failure lives.

---

## Finding 2 — state that quietly lies: wrong answers that looked like right answers, caught only by measurement

Distinct from Finding 1. Here the checks were fine; the *runtime state* returned
a plausible-but-wrong value and carried on. Nothing looked broken. These were
caught by measuring outputs against expected numbers, not by reading code.

1. **`WinError 6` made `resolve_project_root` silently return `cwd`.** A failed
   OS call was swallowed and the function fell back to the current directory,
   which *is* usually the project root — so it looked correct, until it wasn't,
   and scoping silently attached memories to the wrong project.

2. **`cmd_review` promoted candidates with a bogus `project_id`, breaking the
   loop end to end.** The write succeeded, the row existed, the id was wrong, and
   nothing downstream could find what it had just written. A green write, a dead
   read.

3. **`content_hash` computed on truncated text.** The dedup/identity hash was
   taken after truncation, so two different inputs that happened to share a prefix
   hashed identically — a correct-looking hash over the wrong bytes.

4. **The `initialized_at` artifact produced a false `candidates=0`.** A
   timestamp/initialization detail made the candidate count read as zero when it
   wasn't — a confident, specific, wrong number.

**The defense this produced:** declare the red state and watch it fail *first*.
Before trusting a green, force the condition that should make it red and confirm
it actually does. A number that can't be made to lie under a known-bad input is
the only number worth believing.

---

## Finding 3 — three environmental constraints arrived mid-build, each made the tool smaller, and the trial ended it

None of these was a mistake. Each was a real constraint discovered during the
build, and each one removed a layer. What survived every cut was the
deterministic part — which is the part worth noticing.

- **Hooks blocked at the application layer.** `allowManagedHooksOnly:true` meant
  ccmem's capture/inject hooks never ran. Injection moved to `@import` of files
  written by a `generate` step (FACTS.md §6). The entire event-driven design was
  routed around.
- **No `ANTHROPIC_API_KEY`.** The intended memory-extraction path — an LLM
  deciding what was worth remembering — had no key to call. That layer was
  unavailable from the start.
- **Heuristic extraction measured dead.** The fallback, lexical/heuristic
  candidate scoring, was measured: **48 candidates out of 1,959 conversation
  pairs**, dominated by lexical false positives. Not "promising but noisy" —
  not usable. Measured, not asserted.

What was left after all three cuts was the deterministic core: the explicit
`!mem:` sigil and byte-stable `@import` injection. Then the dogfooding trial
tested the one thing that still mattered: *would the author reach for `!mem:`
unprompted in real work?* The answer was no — not because the sigil was wrong,
but because **Claude Code's native auto-memory already covered the case**
automatically, with the real content, no command, no setup (FACTS.md §6). ccmem's
only genuine advantages over native memory — determinism, redaction-on-write, a
supersession audit trail — all live in team/multi-agent/cloud territory that
ccmem's own non-goals excluded. Inside its declared scope it was dominated. That
ended the project.

---

## Corrections against interest

The conclusions above are trustworthy specifically because the project reversed
itself when the evidence said to. Three on the record:

- **The compaction claim was retracted** once a control was added (Finding 1.2;
  FACTS.md §6). The convenient result was wrong and was marked wrong.
- **The Defender exclusion was signal read into noise.** Two days were spent
  tuning an antivirus path exclusion that appeared to cut hook latency ~40%. A
  proper distribution (≥8 runs/session) showed the effect sat *inside*
  run-to-run variance; the exclusion-gone p50 was lower than the
  exclusion-applied p50 the day before. The lever was never real, and the
  project said so and adopted a rule: no latency claim without a distribution
  (FACTS.md §11, "within measurement noise").
- **The worktree assumption was corrected.** `git rev-parse --show-toplevel`
  returns a linked worktree's own directory, not the main repo root; the fix was
  `--git-common-dir` (FACTS.md §4). The original scoping would have mis-keyed
  every worktree.

---

## What survived

The pure-POSIX-shell commit guard: `.githooks/` (`pre-commit`, `pre-push`,
`commit-msg`, `tag`, setup + self-test) and the CI mirror `verify.yml`. It blocks
restricted identifiers in commit identity, messages, content, filenames, and ref
names, with gitleaks/TruffleHog layered on for secrets. During retirement it was
hardened with the two fixes from Finding 1 (fail-closed on a missing scanner;
scan its own source), each with a self-test. It does not depend on anything that
was removed.

## What I'd want to know before building anything like this again

- **Check the platform's native capability first.** Native auto-memory already
  did the job; the tool was partly built against a problem the platform was
  quietly solving in parallel. One afternoon spent characterizing the native
  surface would have reframed the whole project.
- **Confirm the execution model is actually available before designing around
  it.** The hook-driven architecture was designed before `allowManagedHooksOnly`
  was known to block it. The single most expensive assumption was "hooks will
  run."
- **Measure the cheap fallback early.** The 48/1,959 heuristic result could have
  been produced in week one. It would have gated the entire extraction design.
- **A green suite is a coverage map, not a correctness proof.** Budget time to
  read what the checks *exclude*, and to force red states before trusting green.

## If we revisit

The trigger is specific: if native auto-memory starts failing in a *nameable*
way — `MEMORY.md` growing past useful size, a stale entry misleading the
assistant, or a preference from one project leaking into another — then the thing
to build is a **layer that curates `MEMORY.md`**, not a second parallel store.
The cardinal error here was duplication; the only defensible re-entry is to
improve the native store in place, not to compete with it.

The risk to weigh before doing so: `MEMORY.md` is Anthropic's, its format is
undocumented internally, and it **changed twice during this project**. A tool
that writes to it is building on moving ground — which is exactly the kind of
silent-drift exposure Finding 1 is about. Read the format's exclusions before
trusting them.

---

## v2 — attempted and stopped before any code (2026-10-02)

A rebuild was proposed: persistent memory that *supplements* native auto-memory
instead of duplicating it. It was stopped before a line of code was written.

**`permissions.deny` cannot be the capture mechanism.** A deny rule is a veto on a
proposed tool call — purely *subtractive*. It has no execution step, no return
value, no write path, and no channel into the prompt. Capture and injection are
*additive*. So the v1 capture constraint is **unchanged**: hooks blocked, no API
key, heuristics dead — nothing new captures automatically, and any v2 that
maintains its own store dies exactly the way v1 did. The only surviving shape is a
*read-only curator of the native store*, which raised the real question — are
native memory's gaps actually biting?

**That question was answered by reading, not by building the tool that reads.**
The native stores were inspected by hand across ~20 projects. Every `MEMORY.md`
index sits far under the startup-load window; the largest (CCGate) is **17 lines /
5.2 KB against a 200-line / 25.6 KB limit**. Gaps 1–2 (token budget, ranked
retrieval) are *structurally incapable* of biting at that size. Gaps 3–4
(supersession, scoping) appear only as duplicate or stale entries in abandoned
per-directory stores — and because native memory is per-directory, those are
untidiness in dead directories, not wrong facts in live sessions.

**Verdict: native memory is sufficient. The gaps are scale failures that have not
arrived.** Building the curator now would be building for a problem that does not
exist — the v1 mistake. Not built. Cost of reaching this answer: **one day, versus
three weeks** for the same class of answer in v1.

**Sensitivity, stated accurately** (correcting a looser claim made during the
inspection): the hand-read found no real secret — only a placeholder key. Several
stores do hold the author's real name, employer, and git identity. Anthropic does
not transmit auto-memory off the machine — but that is a property of the memory
*system's design*, not of the disk. These files live on a managed laptop with a
cloud-synced profile, and `~/.claude/projects/` also holds transcripts. What the
machine does with that directory is a separate question from what Anthropic does.
Not urgent, not a finding — recorded as what is actually true.

**Why the method is the point.** The question was settled by reading the files
rather than building the thing that reads them — the same discipline as the five
drift findings above, applied *before* the work instead of after. Reading the
exclusion list first is cheaper than discovering the drift later.
