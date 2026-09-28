# Trying out ccmem — a plain-language guide

**No technical background needed.** If you can type a message and copy-paste two
lines, you can do this trial. You cannot break anything — the worst case is a note
doesn't get saved, and nothing is ever deleted behind your back.

---

## What is this, in one sentence?

When you work with **Claude Code** (the AI assistant in your editor/terminal), it
forgets everything the moment a session ends. **ccmem gives it a memory** — a way
for you to say "remember this" so it's still there next time.

Think of it like leaving **sticky notes for the assistant**. Next time you sit
down, it has already read your notes.

## What are we actually testing?

Just one thing: **do you naturally reach for the "remember this" button on your
own?** Not whether the software works (it does) — whether *you* find yourself
wanting it in real work, without anyone reminding you.

So please **don't force it**. Use it only when you genuinely think "I don't want to
explain this again next time." If a whole day goes by and you never reached for it,
that's a real and useful result — write that down too.

---

## The one habit to try

When you're typing a message to Claude Code and something comes up that you'd hate
to re-explain later, **start your message with `!mem:`** and then the thing to
remember.

That's it. Examples:

> `!mem: we decided to use the blue logo, not the green one`

> `!mem: the client hates the word "synergy" — never use it`

> `!mem: my test login is testuser@example.com`

> `!mem: always show me costs in euros, not dollars`

You can keep talking normally in the same message after that line, or just send the
`!mem:` line by itself. Either is fine.

**When should you use it?** Good candidates are decisions ("we chose X"),
preferences ("always do it this way"), corrections ("no, it's actually…"), and
little gotchas you keep having to repeat. If you're unsure — use it. Over-saving is
easy to clean up; forgetting isn't.

---

## Making your notes "stick" — one double-click

Writing `!mem:` records a note, but it needs to be filed away before next time.
That's **one double-click**, no typing:

> **When you're done for the day, double-click `Save my notes`** in your project
> folder. A little window opens, says "Done," and you close it. That's the whole
> ritual — you don't need to do it after every note, just once when you finish.

(If you don't see a `Save my notes` file in your project folder yet, see
**Setup, once** at the bottom — someone sets that up one time and then you never
think about it again.)

---

## Checking it remembered

**The real proof — no typing needed:** start a **new** Claude Code session in the
same project and ask it something your note covered — e.g. "what logo colour did we
agree on?" If it answers from your note, the memory is working.

If a note doesn't show up, it almost always just means the `Save my notes` step
hasn't been done yet for that session.

---

## A few reassurances

- **You can't lose data.** Deleting is reversible, and notes are never overwritten
  silently.
- **Secrets are protected.** If you accidentally paste something like a password or
  key into a `!mem:` note, ccmem refuses to save it. You can see any refused notes
  with `ccmem list --refused`.
- **It's private and local.** Your notes stay on your own machine. Nothing is
  uploaded or shared.

---

## What to tell me after a week or so

This is the important part — a few honest sentences are perfect:

1. **Did you reach for `!mem:` on your own?** Roughly how many times, and did it feel
   natural or like a chore you had to remember?
2. **When you *didn't* use it but maybe should have** — what stopped you? (Forgot?
   Too much friction? Didn't trust it?)
3. **Did a saved note ever actually help later?** A specific example is gold.
4. **Anything confusing or annoying** about the whole thing.

There are no wrong answers. "I kept forgetting it existed" is just as valuable as "I
used it ten times a day" — both tell us what to do next.

---

## Setup, once (for whoever helps get this started)

*This is the only part that needs a technical person, and only one time.*

1. **Install ccmem so the `ccmem` command works everywhere.** From the ccmem repo:
   ```
   pip install -e .
   ```
   (This registers the `ccmem` command via the `[project.scripts]` entry point.
   Undo any time with `pip uninstall ccmem`.)

2. **Drop the one-click saver into each project** the person will trial. Copy
   [`scripts/save-my-notes.cmd`](../scripts/save-my-notes.cmd) into the top folder of
   that project (rename it to `Save my notes.cmd` if you like — Explorer hides the
   `.cmd`). Double-clicking it runs `ccmem capture --latest` then `ccmem generate`
   **in that folder**, so notes are scoped to the right project automatically.

3. **Confirm the `@import` wiring is in place** (it already is in this setup):
   the user's `~/.claude/CLAUDE.md` imports `@ccmem-memories.md` and each project's
   `CLAUDE.md` imports `@.ccmem/memories.md`. That's what makes saved notes reappear
   at the start of the next session.

After that, the daily experience is entirely: type `!mem:` when it helps, and
double-click `Save my notes` when you're done. No terminal.
