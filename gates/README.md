# gates/

Executable acceptance criteria. These are the spec — `docs/PLAN.md` describes what
to build, and these decide whether it's built.

```bash
python gates/run_gates.py --list
python gates/run_gates.py --phase 1
python gates/run_gates.py --only cache_safety
```

Exit 0 only if every selected gate passes.

## The rule

**Do not edit a gate to make it pass.** If a gate encodes the wrong rule, say so and
change it in its own commit, with the reason. A gate quietly loosened is a rule
silently deleted.

## What each one defends

| Gate | Phase | Defends against |
|---|---|---|
| `gate_memory_bank` | 0 | Skipping the manual phase and guessing at the taxonomy |
| `gate_hook_contract` | 1 | A hook wedging the CLI, blocking a prompt, or crashing on bad input |
| `gate_cache_safety` | 1 | Varying injected text silently burning cache writes |
| `gate_secret_hygiene` | 1 | A key landing in a durable store that re-enters context forever |
| `gate_budget` | 1 | Retrieval growing until it makes recall worse |

## Design notes

**Missing implementation is FAIL, not SKIP.** A gate that skips when the file doesn't
exist is a gate that passes a repo with nothing in it.

**No vacuous passes.** `gate_cache_safety` and `gate_budget` seed a 400-row test DB
first, because proving determinism against empty output proves nothing. If
`SessionStart` returns nothing against a seeded DB, that's a failure.

**Empirical before static.** Determinism is checked by running the hook three times
and diffing bytes. The source scan for `time.time()`/`uuid4()`/`random` is a backstop
for code paths the fixtures don't reach — annotate a deliberate, provably-unreachable
use with a trailing `# ccmem: cache-safe` comment.

**Hostile input is the normal case.** Every hook is driven with empty stdin, truncated
JSON, a bare `null`, wrong types, and a 1MB prompt. All must exit 0.

## Config

`gates/config.json` holds hook paths, per-event wall-clock budgets (deliberately
tighter than Claude Code's own timeouts), token and top-K caps, and the injection
marker. Budgets are ceilings, not targets.

## Adding gates

Append to `REGISTRY` in `run_gates.py` with a `min_phase`. Suggested Phase 2+ gates
are commented there already: retrieval determinism, embedding-dimension refusal,
supersession correctness, plugin manifest round-trip, eval regression.

A new gate should fail before the feature exists. Write it first, watch it go red,
then implement.
