# ccmem — daily usage

The 9am cheat sheet. Command-first. There are **no hooks** in this environment
(managed settings block them), so capture and generate are things **you run**.

Run these from **inside the repo** you care about (so project scope resolves to the
git root). All examples use `python -m ccmem.cli`; if you've installed the package,
`ccmem` works too.

---

## The smallest loop (run this once today)

Capture a real decision from a session you just finished, promote it, and see it
load next session:

```bash
cd <your repo>

# 1. Find the transcript(s) waiting to be captured (doctor lists the paths):
python -m ccmem.cli doctor          # look under "transcripts awaiting capture:"

# 2. Capture — scores the transcript's turn-pairs into candidates:
python -m ccmem.cli capture "<transcript path from step 1>"
#    -> candidates=N   (N=0 means nothing scored high enough; see note below)

# 3. Review — promote the good ones to memories:
python -m ccmem.cli review
#    For each candidate: a=accept, r=reject, s=skip.
#    On accept, type the memory text you want kept (or Enter to use the reply verbatim).

# 4. Write the @import files:
python -m ccmem.cli generate

# 5. See it: start a new session in this repo — the memory is in context.
#    Or check now:
python -m ccmem.cli doctor          # "Option E @import health" shows the file + token count
```

**If `capture` reports `candidates=0`**, the session had no clearly-phrased
decision (the scorer looks for phrasings like "we decided", "from now on",
"use X instead of Y", "because"). Just record it directly instead:

```bash
python -m ccmem.cli add --content "chose SQLite over Postgres: local-only, no server to run"
python -m ccmem.cli generate
```

---

## Capturing from a finished session

- The transcript is the session's `.jsonl` under `~/.claude/projects/<munged-cwd>/`.
- **You don't have to find it by hand** — `python -m ccmem.cli doctor` (run from the
  repo) prints the exact paths under **"transcripts awaiting capture"**, plus the
  capture command. That's your discoverability, since no hook does it for you.
- `capture` is idempotent: re-running on the same transcript won't double-add.
- `!mem: <text>` typed in a session is captured as a memory **directly** (no review
  needed). Everything else becomes a *candidate* for review.

```bash
python -m ccmem.cli capture "C:/Users/<you>/.claude/projects/<munged>/<id>.jsonl"
```

## Reviewing & promoting candidates

```bash
python -m ccmem.cli review
```
Interactive, one candidate at a time: `a`ccept / `r`eject / `s`kip. Accepted
candidates become **project-scoped** memories in the current repo. Rejected ones
are marked so they won't reappear.

## Adding a memory directly

```bash
python -m ccmem.cli add --content "..." [--scope project|user|global] \
    [--type decision|preference|correction|project_state|gotcha] [--subject "X"]
```
- Default scope is **project**. `--subject` enables supersession (a newer memory with
  the same subject + scope replaces the older one).

### Choosing scope
| Scope | Use for | Loads in |
|-------|---------|----------|
| **project** | Decisions/gotchas specific to *this* repo | this repo only |
| **global** | Cross-repo facts: machine/env gotchas, infra quirks | every repo |
| **user** | Your personal working preferences | every repo |

`global` and `user` both write to `~/.claude/ccmem-memories.md` (400-token cap);
`project` writes to `<repo>/.ccmem/memories.md` (800-token cap).

## Pinning (survive the token cap)

```bash
python -m ccmem.cli pin <id>          # keep it even when the file hits its cap
python -m ccmem.cli pin <id> --unpin
```

## Managing memories

```bash
python -m ccmem.cli list                 # active memories for this repo + global/user
python -m ccmem.cli show <id>            # full content + context
python -m ccmem.cli delete <id>          # soft-delete (reversible)
python -m ccmem.cli restore <id>
python -m ccmem.cli list --refused       # !mem: entries refused for containing a secret
```

---

## What makes a memory appear next session

`ccmem generate` writes two markdown files that your `CLAUDE.md` files `@import` at
session start (already wired):

- `~/.claude/CLAUDE.md` → `@ccmem-memories.md`  (global + user)
- `<repo>/CLAUDE.md`     → `@.ccmem/memories.md` (project)

**`generate` is NOT automatic.** Run it after you add, promote, delete, or pin —
otherwise the files are stale. Nothing you capture or add shows up until you
`generate`.

## Verifying it worked

```bash
python -m ccmem.cli generate
python -m ccmem.cli doctor     # "Option E @import health": file OK + token count,
                               # @import present, and a WARN if memories were dropped at the cap
```
Then open a new session in the repo — the memories are in context. `doctor` also
warns `only N of M memories shown` when you've exceeded a token cap (prune or `pin`
the ones that matter).
