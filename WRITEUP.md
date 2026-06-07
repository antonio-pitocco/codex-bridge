# An AI that ships better code by arguing with a different AI — and the bugs it caught in one afternoon

*A short write-up of `codex-bridge`: an enforced, adversarial review loop where one
model implements and a different model — sandboxed read-only — verifies. With the
real failures it caught on a production codebase, including one the implementer
introduced while improving the tool itself.*

---

## The problem

A language model reviewing its own work is a sycophant. Ask it "are you sure?" and
it folds or doubles down — either way you learn nothing. "Let me double-check"
inside the same model, same context, is theater.

What you actually want before shipping is an **adversary**: a competent, motivated
critic whose only job is to find what's wrong, who has no stake in your having been
right, and who **cannot quietly fix things to make the disagreement go away.**

## The idea

`codex-bridge` wires two strong models from *different vendors* into an asymmetric
loop:

- **The implementer** (here, Claude) proposes and is the only one who writes code.
- **The reviewer** (here, OpenAI's Codex) runs in a **read-only sandbox**. It can
  read the whole repo and run read-only commands to ground its critique — but the
  CLI runner **blocks its file writes**. Its only available move is to find defects.

They exchange proposals until they reach consensus; then the implementer ships.

The key design decision is that the asymmetry comes from the **sandbox runner, not
the prompt.** "Please only review, don't edit" is a suggestion a model can ignore.
`codex exec -s read-only` makes the runner refuse the writes instead.

Three more things make it more than "two chatbots talking":

1. **Anti-sycophancy can be made structural.** `codex-bridge` ships a tested
   decision function, `consensus_reached`; if your loop uses it, an early `AGREE`
   does not count as consensus until a minimum number of adversarial rounds has
   happened, scaling with risk (`trivial: 1, standard: 2, critical: 3`). So the gate
   refuses to count a round-one `AGREE` for critical work — the rule lives in tested
   code, not in the orchestrator's good intentions. (The bridge exposes the rule;
   your loop has to honor it.)
2. **The verdict is machine-readable and fail-safe.** Only a clean `VERDICT: AGREE`
   on the last line counts. `AGREE with caveats`, text after the verdict, or a
   missing line all parse to `MALFORMED` — *not consensus*. You never ship on an
   ambiguous signal. (This rule itself was a bug once — see below.)
3. **It degrades cleanly.** Every failure mode — timeout, missing binary, invalid
   resume, unwritable temp dir — returns a typed `error_kind` so the loop can tell a
   genuine objection apart from an infrastructure failure.

## What it caught (one afternoon, real codebase)

I dogfooded it: I ran it on its own design, on its own improvements, and on real
bugs in a production publishing pipeline. The reviewer earned its keep.

**It found a bug the implementer introduced while improving the tool itself.**
I "hardened" the verdict parser to use a regex `findall` — which happily accepted
`VERDICT: AGREE` followed by *more text*, turning a qualified non-answer into a
false consensus. The reviewer produced the exact counterexample. I'd written a
consensus-detector that could be fooled into declaring consensus. The fix:
match only the last non-empty line, strictly.

**It stopped me from shipping orphaned files to a nightly production job — twice.**
The pipeline uploaded files to a public path *before* the database write; if the
write failed, the files were orphaned. I wrote a staging-then-finalize fix and a
real server smoke test passed. The reviewer pointed out the orphan bug was *still
there, just moved later*: finalize-then-DB-failure left files orphaned; a partial
finalize on collision left some files public; and the compensating cleanup deleted
the wrong set of rows. Across three review rounds it surfaced ~13 distinct
failure-path defects — the kind that only bite on a rare production failure, which
is exactly when you can't afford them.

**It told me my initial fix was conceptually wrong.** For a "don't create duplicate
records" change, I proposed reusing an existing code path. The reviewer read that
path and showed me it only fired under a condition that didn't hold here — my fix
would not have worked. I'd have shipped a no-op believing it was a fix.

**It flagged a dormant security default.** While reviewing, it noticed a
content-inspection guard (the only defense against accidentally publishing
third-party personal data — a real incident in this codebase's past) was
*off by default*. Not an active leak, but a trap primed to spring.

The pattern in all of these: I was confident, the work looked done, the happy-path
tests were green — and a motivated adversary that could read but not write found the
holes anyway.

## Why the anti-sycophancy floor mattered

In every round the process *refused to accept* early agreement — the gate didn't
count an `AGREE` as consensus until enough adversarial rounds had happened. That
single rule is what forced genuine iteration instead of a polite "looks good." On
the verdict-parser bug, a one-round rubber stamp would have shipped it. The floor is
the difference between a real adversary and a flattering mirror.

## Honest limitations

This is not magic and I won't oversell it.

- **It's slow.** A reviewer at high reasoning effort is minutes per round; a
  critical topic is three-plus rounds. Worth it for a gnarly production change, not
  for a typo.
- **The proposer-judge conflict is mitigated, not eliminated.** The implementer
  still decides which objections to accept. A stubborn or wrong-confident
  implementer can still rationalize. The sandbox enforces *who writes*; it can't
  enforce *good judgment*.
- **It depends on the Codex CLI**, whose flags can drift between versions (there's a
  skippable test that pins the contract).
- I have mostly seen the case where the reviewer was right and I deferred. I have
  not yet stress-tested the harder case where the reviewer is wrong and the
  implementer must correctly overrule it.

## The takeaway

The reusable idea isn't "make two models chat." It's: **for work that's expensive to
get wrong, put a competent adversary from a different model in the loop, give it the
ability to read everything and the inability to change anything, and don't let it
agree with you cheaply.** The implementation here is ~250 lines of Python around a
non-interactive CLI. The discipline — actually routing your production changes
through the adversary before shipping — is the part that pays.

`codex-bridge` is MIT-licensed. The whole thing is one file and a test suite.

---

*If you build agents that ship code, the most useful thing this might give you is
not the code but the habit: before you commit, let something that can't flatter you
read the diff.*
