# codex-bridge

**Enforced adversarial code review between two models.** One model implements; a
*different* model — sandboxed **read-only** — verifies. They exchange proposals
until they reach consensus, then the implementer ships. The asymmetry is enforced
by the sandbox, not by a polite instruction in the prompt.

A model reviewing its own work is sycophantic. `codex-bridge` makes the reviewer
a **different vendor's model** that physically cannot edit your files — so its job
is only to find what's wrong. It's small (one file), headless, and tested.

> Built and battle-tested by driving an [OpenAI Codex CLI](https://github.com/openai/codex)
> reviewer against a Claude implementer on a real production codebase. See
> [`WRITEUP.md`](WRITEUP.md) for the bugs it caught — including one the implementer
> introduced *while improving the tool itself*.

## Why this and not "ask the model to double-check"

- **The reviewer cannot write.** It runs in a `read-only` sandbox (`codex exec -s read-only`).
  It can read your repo and run read-only commands to ground its critique, but it
  cannot touch a single file. Only the implementer ships. This is the core idea.
- **Anti-sycophancy is structural, not a vibe.** Consensus requires a *minimum
  number of adversarial rounds* that scales with the topic's risk
  (`MIN_ROUNDS = {trivial: 1, standard: 2, critical: 3}`) — so the reviewer can't
  rubber-stamp on round one. This is tested code (`consensus_reached`), not the
  orchestrator's discipline.
- **The verdict is machine-readable and fail-safe.** `parse_verdict` only accepts a
  clean `VERDICT: AGREE` as the *last line*; anything else (`AGREE with caveats`,
  text after the verdict, missing) is `MALFORMED` — i.e. *not consensus*. You never
  ship on an ambiguous signal.
- **It degrades cleanly.** Timeouts, a missing/inexecutable `codex` binary, an
  invalid resume, an unwritable temp dir — each returns a typed `error_kind`
  instead of crashing, so an orchestrator can branch deterministically.

## Install

```bash
pip install codex-bridge   # once published
# or, from a checkout:
pip install -e .
```

Requires the [Codex CLI](https://github.com/openai/codex) on your `PATH`
(`codex exec` is the non-interactive entry point this driver uses).

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
   true, or you hit a round cap (3 for trivial/standard, 5 for critical) → escalate
   to a human. Only on consensus does the implementer ship.

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

## Output schema

Each round prints/returns `{thread_id, exit_code, error, error_kind, ok, verdict, text}`.
Read `ok` (exit 0 **and** non-empty text) as the sanity signal *before* `text`.
`error_kind` ∈ `timeout | codex_missing | resume_invalid | empty_output | temp_unavailable | run_error`.

## Status & honest limits

- It is **slow** (a reviewer at high reasoning effort is minutes per round). Use
  `--effort low/medium` for light topics.
- It depends on the **Codex CLI** (its flags can drift between versions — there is a
  skippable integration test that pins the contract).
- The proposer-judge conflict is **mitigated, not eliminated**: the implementer
  still decides which objections to accept. Use it where a second, adversarial
  opinion is worth minutes — not for trivial edits.

## License

MIT © 2026 Antonio Pitocco

## Tests

```bash
python -m pytest tests/ -q          # hermetic unit tests (no real Codex)
CODEX_BRIDGE_IT=1 python -m pytest tests/test_integration.py -q   # pins the real CLI contract
```
