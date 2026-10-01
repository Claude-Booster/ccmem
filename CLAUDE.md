# ccmem (retired)

This repository is retired. It once held **ccmem**, a persistent-memory tool for
Claude Code — built, trialled, and withdrawn once Claude Code's native
auto-memory proved to already cover the case. What remains is a commit-time
identifier/secret guard and the design reasoning behind the memory work.
**Read [docs/POSTMORTEM.md](docs/POSTMORTEM.md) first** for why it was retired;
the surviving design docs (`docs/DESIGN.md`, `docs/FACTS.md`, `docs/PLAN*.md`,
`docs/BRIEF.md`) are the evidence it cites. The live guard is the `.githooks/`
suite in this repo — there is no separate repo for it.

## Commands

```bash
bash .githooks/setup-hooks.sh     # activate the commit guard in a fresh clone
bash .githooks/test-hooks.sh      # guard self-test (should pass 10/10)
```

## The guard

Pure-POSIX-shell git hooks (`pre-commit`, `pre-push`, `commit-msg`, `tag`) that
block restricted identifiers — names, emails, employer, internal domains — in
commit identity, messages, file content, filenames, and ref names, with
gitleaks/TruffleHog layered on for secrets (`.github/workflows/verify.yml`
mirrors the identifier scan server-side). Real patterns live in
`.githooks/.blocked`, which is gitignored and never committed; the hooks carry
only opaque labels. The secret scan is fail-closed — a missing scanner refuses
the commit unless `CCMEM_ALLOW_NO_SCANNER=1` is set. Copy
`.githooks/.blocked.example` to `.githooks/.blocked` and fill in real values
before relying on it.

## Working style

- **Patch, don't replace.** Use targeted edits. I often have edits in flight in
  the same files; a full-file rewrite silently discards them.
- **Gates over prose.** A rule that isn't a runnable check is a suggestion.
- **Python 3.11+, stdlib-first.** Third-party deps need a reason.
