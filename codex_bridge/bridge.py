"""Headless driver to talk to the Codex CLI (gpt-5.x) programmatically.

Uses `codex exec` (NOT the interactive TUI, which is fragile to drive):
- the reviewer's messages are captured from the `--json` event stream;
- the `thread_id` is extracted from the `--json` events (`thread.started`);
- context across rounds is preserved with `codex exec resume <thread_id>`.

Default sandbox = **read-only**: the reviewer model can read the repo and run
read-only commands, but CANNOT write files. This is the core idea: the rule
"only the implementer touches the code" is enforced by the sandbox, not by a
polite instruction in the prompt.

CLI:
    echo "prompt" | python3 -m codex_bridge ask
    python3 -m codex_bridge verify --prompt-file p.txt --thread <id> --effort low
Output: a single JSON line {thread_id, exit_code, error, error_kind, ok, verdict, text}.
"""
from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Verdict must be EXACTLY the last non-empty line (not a match anywhere in the
# text — otherwise text AFTER the verdict would be ignored and a qualifying tail
# would become a false agreement). fullmatch on the stripped last line.
_VERDICT_RX = re.compile(r"VERDICT:[ \t]*(AGREE|OBJECT)", re.IGNORECASE)


def parse_verdict(text: str) -> str:
    """Verdict from the LAST non-empty line: `VERDICT: AGREE|OBJECT`.

    Returns 'AGREE', 'OBJECT' or 'MALFORMED'. Strict by design: the verdict MUST
    be the last line (text after it -> MALFORMED), and the line must match
    entirely (e.g. "VERDICT: AGREE with caveats" -> MALFORMED). Fail-safe:
    anything that is not a clean AGREE is not consensus. Tested code.
    """
    lines = (text or "").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return "MALFORMED"
    m = _VERDICT_RX.fullmatch(lines[-1].strip())
    return m.group(1).upper() if m else "MALFORMED"


# Sandboxes accepted by `codex exec`. read-only is the default of a review:
# the reviewer verifies, it does NOT write. workspace-write/danger must NOT be
# used in a review (they would break "only the implementer touches the code").
VALID_SANDBOX = ("read-only", "workspace-write", "danger-full-access")
# Reasoning levels. None = respect ~/.codex/config.toml.
VALID_EFFORT = ("low", "medium", "high", "xhigh")


@dataclass
class CodexReply:
    """One round of the reviewer's reply."""
    #: ALL assistant messages of the turn, in order — not just the last one.
    #: `--output-last-message` writes only the final message, and a reviewer
    #: often closes a turn with a short wrap-up: capturing only that silently
    #: replaces the audit with its summary. See `_extract_agent_messages`.
    text: str
    thread_id: str | None      # session id, to pass to the next round
    exit_code: int
    error: str = ""
    #: timeout|codex_missing|resume_invalid|run_error|empty_output|
    #: temp_unavailable|degraded_last_only
    error_kind: str = ""

    @property
    def ok(self) -> bool:
        """A DEGRADED round is not a valid round.

        If the `--json` stream is truncated we can only fall back to the final
        message — which is exactly the failure mode this driver exists to
        avoid. Reporting it as `ok` would make the degradation visible but not
        authoritative: an orchestrator that only skips `ok == false` rounds
        would accept a wrap-up as if it were the audit.
        """
        return (self.exit_code == 0 and bool(self.text)
                and self.error_kind != "degraded_last_only")


# ADAPTIVE convergence: minimum adversarial rounds before the reviewer's AGREE
# counts as consensus. Scales with topic complexity instead of a fixed floor
# (a blunt "always >=3 rounds" wastes time on simple topics). Tested code,
# not the orchestrator's discipline.
MIN_ROUNDS = {"trivial": 1, "standard": 2, "critical": 3}


def consensus_reached(rounds_done: int, complexity: str, reviewer_verdict: str,
                      implementer_satisfied: bool) -> bool:
    """True if the debate may close with unanimous consensus.

    - `rounds_done`: number of VALID reviewer verdicts received so far.
    - `complexity`: 'trivial' (creative/idea) | 'standard' | 'critical' (code/legal).
    - consensus = clean AGREE from the reviewer AND implementer satisfied AND at
      least MIN_ROUNDS[complexity] adversarial rounds done (adaptive anti-sycophancy).
    A trivial topic can close in 1 round; critical code needs >=3.
    """
    key = (complexity or "").strip().lower()
    if key not in MIN_ROUNDS:
        # Fail-safe: an unrecognized complexity (typo, casing) must NOT lower the
        # bar -> treat it as 'critical' (highest floor).
        key = "critical"
    verdict = (reviewer_verdict or "").strip().upper()
    return verdict == "AGREE" and implementer_satisfied and rounds_done >= MIN_ROUNDS[key]


# INDICATIVE ceiling of rounds. It is an attention threshold, not a ban:
# see `should_continue_past_cap`.
ROUND_CAP = {"trivial": 3, "standard": 3, "critical": 5}


def should_continue_past_cap(
    rounds_done: int,
    complexity: str,
    last_round_new_accepted_defects: int,
    disagreement_is_on_merit: bool,
) -> bool:
    """True if the debate MUST continue past the indicative round ceiling.

    The cap exists to stop unproductive loops, but a pure round count can do the
    opposite damage: on a real review, five rounds produced 33 genuine defects
    and the fifth round still found two more — both accepted without pushback —
    yet the rule said "stop". Stopping while the reviewer is still finding real
    defects is the opposite of why the loop exists.

    The discriminator is not the round number but the nature of the last round:

      * the last round produced new defects that were **accepted and fixed**
        (not disputed) -> the debate is still producing value: CONTINUE;
      * the last round produced no accepted defects -> either it is a loop, or
        the remaining objections are disputed: STOP and escalate to a human with
        the residual disagreement, which at that point is genuinely on the merits.

    `disagreement_is_on_merit` is the explicit valve: if the divergence is about
    a judgement call and not a defect, more rounds will not dissolve it.
    """
    if disagreement_is_on_merit:
        return False
    if rounds_done < ROUND_CAP.get((complexity or "").strip().lower(), 5):
        return True          # below the ceiling we continue anyway
    return last_round_new_accepted_defects > 0


def _codex_bin() -> str:
    return shutil.which("codex") or os.path.expanduser("~/.npm-global/bin/codex")


def _extract_thread_id(stdout: str | None, fallback: str | None) -> str | None:
    """Extract thread_id from the thread.started/resumed JSONL event of `--json`."""
    tid = fallback
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") in ("thread.started", "thread.resumed") and ev.get("thread_id"):
            tid = ev["thread_id"]
    return tid


def _extract_agent_messages(stdout: str | None) -> tuple[list[str], bool]:
    """(COMPLETED assistant messages in order, is the stream intact?).

    Event shape verified live against the `--json` output, not inferred:
    `{"type":"item.completed","item":{"type":"agent_message","text":"…"}}`.

    This exists because `--output-last-message` writes ONLY the final message:
    when the reviewer emits a long analysis and then a short closing wrap-up,
    a driver reading that file hands you the wrap-up and throws the analysis
    away. A reviewer that delivers its summary instead of its audit is a
    safeguard that has been silently switched off.

    **Only `item.completed`.** Matching on `item.type` alone also accepted
    `item.updated` partials, and then: a partial duplicated the text of the
    completion that followed it, and a partial left alone suppressed the
    fallback because `text` was not empty.

    The second value is `False` when the stream is NOT intact — a truncated
    JSON line, or a missing `turn.completed`. The caller must be able to
    declare that instead of presenting a partial audit as a complete one.
    """
    out: list[str] = []
    intact = False
    malformed = False
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if not isinstance(ev, dict):
            malformed = True
            continue
        if ev.get("type") == "turn.completed":
            intact = True
            continue
        if ev.get("type") != "item.completed":
            continue
        item = ev.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text.strip())
    return out, (intact and not malformed)


def _classify_error(returncode: int, stderr: str) -> str:
    """Classify the failure so the caller can branch deterministically.

    Only `resume_invalid` is confirmed by the CLI's stderr format; the others
    are derived from the exit code. Not an exhaustive classifier: a useful label.
    """
    s = (stderr or "").lower()
    if returncode == 124:
        return "timeout"
    if returncode == 127:
        return "codex_missing"
    if "no rollout found" in s or "code -32600" in s:
        return "resume_invalid"
    return "run_error"


def _build_args(codex, thread_id, sandbox, cwd, model, effort, last_path) -> list[str]:
    """Build the `codex exec`/`resume` argv (pure, testable without a filesystem).

    resume inherits cwd/sandbox from the original session (it does not accept
    -C/-s) but we re-assert `sandbox_mode` via -c (verified live: accepted on
    resume) as defense in depth. Round 1 uses explicit -C/-s/--color.
    """
    out_opts = ["-o", last_path, "--json"]
    if model:
        out_opts += ["-m", model]
    if effort:
        # -c parses the value as TOML: quoted string -> string literal.
        out_opts += ["-c", f'model_reasoning_effort="{effort}"']
    if thread_id:
        return [codex, "exec", "resume",
                "-c", f'sandbox_mode="{sandbox}"', *out_opts, thread_id, "-"]
    return [codex, "exec", "-C", str(cwd), "-s", sandbox, "--color", "never", *out_opts, "-"]


def _terminate_process_tree(proc: subprocess.Popen, grace_seconds: float = 0.5) -> None:
    """Kill the reviewer process AND its children, leaving no live pipe or orphan.

    On POSIX `_run_codex_process` starts a new session, so the process PID is
    also the process-group ID: the timeout hits the whole group, first with
    TERM and then with KILL. Without this, a grandchild that ignores TERM keeps
    the pipes open and `communicate()` blocks well past the timeout — i.e. the
    timeout silently does not exist. The direct-process fallback is only for
    platforms without POSIX process groups.
    """
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            # Conservative: if the group is unreachable, at least the direct
            # process must not stay alive.
            try:
                proc.terminate()
            except OSError:
                pass
    else:
        try:
            proc.terminate()
        except OSError:
            pass

    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass

    if os.name == "posix":
        # Even if the parent already exited on TERM, a descendant may have
        # ignored it and still hold the pipes. KILL on the group is therefore
        # unconditional, not only when `proc.wait` times out.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
    elif proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    if proc.poll() is None:
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            # Do not wait indefinitely on the timeout path itself.
            pass


def _run_codex_process(
    args: list[str],
    prompt: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run the reviewer with a real timeout over the whole process tree."""
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial_stdout = exc.stdout
        partial_stderr = exc.stderr
        _terminate_process_tree(proc)
        # After KILL the group's pipes must close. Do not turn a timeout into a
        # second hang though: one last collection, strictly bounded, and the
        # result stays a closed error either way.
        try:
            tail_stdout, tail_stderr = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            tail_stdout, tail_stderr = "", ""
        if isinstance(partial_stdout, bytes):
            partial_stdout = partial_stdout.decode("utf-8", "replace")
        if isinstance(partial_stderr, bytes):
            partial_stderr = partial_stderr.decode("utf-8", "replace")
        # `communicate()` after the kill normally returns everything buffered,
        # not just a tail: preferring it avoids duplication.
        final_stdout = tail_stdout if tail_stdout else (partial_stdout or "")
        final_stderr = tail_stderr if tail_stderr else (partial_stderr or "")
        raise subprocess.TimeoutExpired(
            args,
            timeout,
            output=final_stdout,
            stderr=final_stderr,
        ) from None
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def ask_codex(
    prompt: str,
    thread_id: str | None = None,
    *,
    sandbox: str = "read-only",
    effort: str | None = None,
    cwd: str | Path | None = None,
    model: str | None = None,
    timeout: int = 1800,
) -> CodexReply:
    """Send `prompt` to the reviewer model and return its messages + thread_id.

    If `thread_id` is set, it resumes that session (the reviewer remembers the
    earlier exchange). The prompt is passed via stdin to avoid escaping problems
    with long/multiline text. `cwd` defaults to the current working directory.
    """
    if sandbox not in VALID_SANDBOX:
        raise ValueError(f"invalid sandbox: {sandbox!r} (allowed: {VALID_SANDBOX})")
    if effort is not None and effort not in VALID_EFFORT:
        raise ValueError(f"invalid effort: {effort!r} (allowed: {VALID_EFFORT})")
    cwd = cwd if cwd is not None else os.getcwd()

    codex = _codex_bin()
    try:
        fd, last_path = tempfile.mkstemp(suffix=".txt", prefix="codex_last_")
        os.close(fd)
    except OSError as e:
        # temp dir not writable -> degrade cleanly instead of crashing.
        return CodexReply("", thread_id, 1, f"temp dir unavailable: {e}", "temp_unavailable")
    args = _build_args(codex, thread_id, sandbox, cwd, model, effort, last_path)

    try:
        p = _run_codex_process(args, prompt, timeout)
    except subprocess.TimeoutExpired as e:
        # Recover the thread_id from the partial stdout: a paid xhigh session
        # stays resumable instead of being lost.
        partial = e.stdout
        if isinstance(partial, (bytes, bytearray)):
            partial = partial.decode("utf-8", "replace")
        _safe_unlink(last_path)
        return CodexReply("", _extract_thread_id(partial, thread_id), 124, "timeout", "timeout")
    except FileNotFoundError as e:
        _safe_unlink(last_path)
        return CodexReply("", thread_id, 127, f"codex not found: {e}", "codex_missing")
    except OSError as e:
        # binary present but not executable (EACCES/ENOEXEC), etc.
        _safe_unlink(last_path)
        return CodexReply("", thread_id, 127, f"codex not executable: {e}", "codex_missing")

    tid = _extract_thread_id(p.stdout, thread_id)
    if p.returncode != 0:
        text = ""  # do not trust the -o file on a failed run (may be stale/partial)
    else:
        # The `--json` stream carries ALL messages of the turn;
        # `--output-last-message` carries only the last one. We take the stream
        # and fall back to the file: the fallback covers a schema change in the
        # CLI, which would otherwise switch the driver off instead of degrading
        # it. Order is emission order, so the last line stays the last line and
        # `parse_verdict` still reads the right verdict.
        messages, intact = _extract_agent_messages(p.stdout)
        text = "\n\n".join(messages).strip()
        if not text or not intact:
            # DECLARED fallback. Undeclared, it degrades to exactly the historic
            # defect — only the final message — and does so silently, i.e. the
            # loop goes back to reading a wrap-up believing it is the audit.
            try:
                last = Path(last_path).read_text(
                    encoding="utf-8", errors="replace").strip()
            except OSError:
                last = ""
            if not text:
                text, degraded = last, bool(last)
            else:
                degraded = True
            if degraded:
                _safe_unlink(last_path)
                return CodexReply(text=text, thread_id=tid, exit_code=0,
                                  error="incomplete --json stream: text may be "
                                        "the final message only",
                                  error_kind="degraded_last_only")
    _safe_unlink(last_path)

    if p.returncode != 0:
        err = (p.stderr.strip() or "exit != 0")[:500]
        return CodexReply("", tid, p.returncode, err, _classify_error(p.returncode, p.stderr))
    if not text:
        # exit 0 but no readable message: silent failure -> explicit diagnostic.
        return CodexReply("", tid, 0, "ok but no readable agent message", "empty_output")
    return CodexReply(text=text, thread_id=tid, exit_code=0)


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _add_common_args(p) -> None:
    p.add_argument("--prompt", help="inline prompt (else --prompt-file or stdin)")
    p.add_argument("--prompt-file", help="file to read the prompt from")
    p.add_argument("--thread", help="thread_id to resume the session")
    p.add_argument("--model", help="reviewer model override (e.g. gpt-5.5)")
    p.add_argument("--effort", choices=list(VALID_EFFORT),
                   help="reasoning effort (default: CLI config). low/medium for simple tasks")
    p.add_argument("--timeout", type=int, default=1800)


def _resolve_prompt(ns, ap) -> str:
    import sys
    if ns.prompt:
        prompt = ns.prompt
    elif ns.prompt_file:
        try:
            prompt = Path(ns.prompt_file).read_text(encoding="utf-8")
        except OSError as e:
            ap.error(f"--prompt-file unreadable: {e}")
    else:
        prompt = sys.stdin.read()
    if not prompt.strip():
        ap.error("empty prompt")
    return prompt


def _emit(r: CodexReply) -> int:
    print(json.dumps({
        "thread_id": r.thread_id,
        "exit_code": r.exit_code,
        "error": r.error,
        "error_kind": r.error_kind,
        "ok": r.ok,
        "verdict": parse_verdict(r.text) if r.ok else None,
        "text": r.text,
    }, ensure_ascii=False))
    return 0 if r.ok else 1


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="codex_bridge", description="One headless round with the reviewer CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="send a prompt to the reviewer (configurable sandbox)")
    _add_common_args(a)
    a.add_argument("--sandbox", default="read-only", choices=list(VALID_SANDBOX))

    # `verify` = the review path: read-only, NON bypassable (no --sandbox). The
    # reviewer is always and only a verifier, never a writer.
    v = sub.add_parser("verify", help="a review round: forced read-only (for the debate loop)")
    _add_common_args(v)

    ns = ap.parse_args(argv)
    prompt = _resolve_prompt(ns, ap)
    sandbox = "read-only" if ns.cmd == "verify" else ns.sandbox
    r = ask_codex(prompt, ns.thread, sandbox=sandbox, effort=ns.effort,
                  model=ns.model, timeout=ns.timeout)
    return _emit(r)


if __name__ == "__main__":
    raise SystemExit(main())
