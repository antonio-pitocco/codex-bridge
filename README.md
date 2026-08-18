# codex-bridge

**Enforced adversarial code review between two models.** One model implements; a
*different* model — sandboxed **read-only** — verifies. They exchange proposals
until they reach consensus, then the implementer ships. The asymmetry is enforced
by the sandbox, not by a polite instruction in the prompt.

A model reviewing its own work is sycophantic. `codex-bridge` makes the reviewer
a **different vendor's model** whose file writes are blocked by the Codex CLI
read-only sandbox in normal operation — so its job is only to find what's wrong.
`codex-bridge` is the small (one-file) *primitive*; the debate loop is a thin layer
you drive on top of it (see below).

> Built and battle-tested by driving an [OpenAI Codex CLI](https://github.com/openai/codex)
> reviewer against a Claude implementer on a real production codebase. See
> [`WRITEUP.md`](WRITEUP.md) for the bugs it caught — including one the implementer
> introduced *while improving the tool itself*.

## Why this and not "ask the model to double-check"

- **The reviewer doesn't write.** It runs in a `read-only` sandbox (`-s read-only`):
  the Codex CLI runner blocks file writes, so the reviewer can read your repo and run
  read-only commands to ground its critique but does not edit it. Only the implementer
  ships. This is the core idea — the asymmetry comes from the runner, not a prompt.
- **Anti-sycophancy can be made structural.** `codex-bridge` ships a tested
  decision function, `consensus_reached`, that refuses to count an `AGREE` as
  consensus until a *minimum number of adversarial rounds* has happened, scaling
  with risk (`MIN_ROUNDS = {trivial: 1, standard: 2, critical: 3}`). If your loop
  uses it, the reviewer can't rubber-stamp critical code on round one.
- **The round ceiling is a threshold, not a ban.** `should_continue_past_cap`
  keeps the debate going *past* the nominal cap for as long as the reviewer is
  still producing defects you accept, and stops it when it isn't. Counting rounds
  is the wrong stop condition (see below).
- **You get the audit, not its summary.** The reviewer's messages are read from
  the `--json` event stream, so a long analysis followed by a short wrap-up
  returns *both*. A truncated stream is reported as `degraded_last_only` and is
  **not a valid round** — never a silent downgrade.
- **The verdict is machine-readable and fail-safe.** `parse_verdict` only accepts a
  clean `VERDICT: AGREE` as the *last line*; anything else (`AGREE with caveats`,
  text after the verdict, missing) is `MALFORMED` — i.e. *not consensus*. You never
  ship on an ambiguous signal.
- **It degrades cleanly.** Timeouts, a missing/inexecutable binary, an invalid
  resume, an unwritable temp dir — each returns a typed `error_kind` instead of
  crashing, so an orchestrator can branch deterministically. The timeout kills the
  whole process group, so a child that ignores `SIGTERM` can't hold the pipes open
  past the deadline.

## Install

```bash
pip install codex-bridge   # once published
# or, from a checkout:
pip install -e .
```

Requires the [Codex CLI](https://github.com/openai/codex) on your `PATH`
(the non-interactive `exec` entry point is what this driver uses).

## Quickstart

```bash
# One review round. The prompt goes via stdin (no escaping headaches).
echo "Review the diff in this repo for correctness bugs." | codex-bridge verify --effort medium
# -> {"thread_id":"019...","ok":true,"verdict":"OBJECT","error_kind":"","text":"...findings..."}

# Continue the same session (the reviewer remembers the previous round):
codex-bridge verify --thread 019... --prompt-file round2.txt --effort medium
```

`verify` forces `read-only` and cannot be bypassed. `ask` is the general form
(configurable sandbox) for other uses.

Programmatic:

```python
from codex_bridge import ask_codex, parse_verdict, consensus_reached

r = ask_codex("Find real bugs in the staged changes.", cwd=".")   # read-only by default
print(r.ok, parse_verdict(r.text))

# Resume to keep context across rounds:
r2 = ask_codex("I fixed X and Y. Verify.", thread_id=r.thread_id)
```

## The debate loop (the methodology)

The bridge is the primitive; the loop is how you use it. One round = one call.

1. **Classify the topic**: `trivial` (a draft, an idea), `standard`, or `critical`
   (production code, anything irreversible). This sets the consensus floor.
2. **Round 0** — the implementer writes a concrete position (a design, a diff).
3. **Send it** to the reviewer with the framing below; the reviewer critiques and
   ends with a machine-readable verdict.
4. **Read the gate first**: check `ok`/`error_kind` *before* `text` — a failure is
   not an opinion and must not count as a round. Then read `verdict`.
5. **Revise and resend** via `resume` until
   `consensus_reached(rounds_done, complexity, verdict, implementer_satisfied)` is
   true. At the round ceiling, don't stop on the count — ask
   `should_continue_past_cap(...)` (below). Only on consensus does the implementer ship.

Suggested reviewer framing (each round ends with the verdict line):

```
You are the SEVERE REVIEWER. You run read-only: read the repo and run read-only
commands, but you do NOT write files — the implementer does. Find REAL defects.
Do not be agreeable: cite file:line, data, counterexamples. If the topic is code
and you have a better approach, sketch your own concrete diff.

Close the LAST line EXACTLY with one of:
VERDICT: AGREE
VERDICT: OBJECT
```

### When to stop: defects, not round count

`ROUND_CAP` (3 for trivial/standard, 5 for critical) exists to stop unproductive
loops, but a pure round count can do the opposite damage. On a real review, five
rounds produced 33 genuine defects — and the fifth round still found two more,
both accepted without pushback — while the rule said "stop". Stopping while the
reviewer is still finding real defects is the opposite of why the loop exists.

```python
from codex_bridge import should_continue_past_cap

while True:
    ...  # run a round, count the defects you actually accepted and fixed
    if consensus_reached(rounds, complexity, verdict, satisfied):
        break
    if not should_continue_past_cap(rounds, complexity,
                                    new_accepted_defects_last_round,
                                    disagreement_is_on_merit):
        escalate_to_a_human()      # a loop, or a genuine judgement call
        break
```

The discriminator is the *nature of the last round*: if it produced new defects
you accepted, the debate is still paying for itself — continue. If it didn't,
it's either a loop or a disputed judgement call, and more rounds won't dissolve
it; `disagreement_is_on_merit` is the explicit valve for that.

## Output schema

Each round prints/returns `{thread_id, exit_code, error, error_kind, ok, verdict, text}`.
Read `ok` (exit 0, non-empty text, **and** not degraded) as the sanity signal
*before* `text`. `error_kind` ∈
`timeout | codex_missing | resume_invalid | empty_output | temp_unavailable | run_error | degraded_last_only`.

`text` contains **every** assistant message of the turn, joined in emission order
— so the verdict is still the last line, and `parse_verdict` still works. If the
event stream is truncated the driver falls back to the final message only, but
marks the round `degraded_last_only` with `ok == false`: a wrap-up must never be
mistaken for the audit.

## Status & honest limits

- It is **slow** (a reviewer at high reasoning effort is minutes per round). Use
  `--effort low/medium` for light topics.
- It depends on the **Codex CLI** (its flags can drift between versions — there is a
  skippable integration test that pins the contract).
- The proposer-judge conflict is **mitigated, not eliminated**: the implementer
  still decides which objections to accept. Use it where a second, adversarial
  opinion is worth minutes — not for trivial edits.
- `should_continue_past_cap` takes *your* count of accepted defects. It mechanizes
  the stop rule; it can't tell you whether you were honest about the count.

## License

MIT © 2026 Antonio Pitocco

## Tests

```bash
pip install -e ".[test]"
python -m pytest tests/test_bridge.py -q   # hermetic unit tests (no real reviewer calls)
# CLI-contract integration tests (pin the exec/resume flags this driver relies on;
# skipped unless enabled — they check the flag contract, not a full run):
CODEX_BRIDGE_IT=1 python -m pytest tests/test_integration.py -q
```

All unit tests are mocked except the process-group timeout test, which spawns
real short-lived processes: that a `SIGTERM`-ignoring grandchild dies with the
group is not a property you can prove with a mock.
