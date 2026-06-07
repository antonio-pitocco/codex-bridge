"""Headless driver to talk to the Codex CLI (gpt-5.x) programmatically.

Uses `codex exec` (NOT the interactive TUI, which is fragile to drive):
- Codex's final message is captured cleanly via `-o <file>`;
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
    text: str                  # final message (the reviewer's opinion)
    thread_id: str | None      # session id, to pass to the next round
    exit_code: int
    error: str = ""
    error_kind: str = ""       # timeout|codex_missing|resume_invalid|run_error|empty_output|temp_unavailable

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and bool(self.text)


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


def _classify_error(returncode: int, stderr: str) -> str:
    """Classify the failure so the caller can branch deterministically.

    Only `resume_invalid` is confirmed by codex's stderr format; the others are
    derived from the exit code. Not an exhaustive classifier: a useful label.
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
    """Send `prompt` to the reviewer model and return its final message + thread_id.

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
        p = subprocess.run(args, input=prompt, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
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
        try:
            text = Path(last_path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
    _safe_unlink(last_path)

    if p.returncode != 0:
        err = (p.stderr.strip() or "exit != 0")[:500]
        return CodexReply("", tid, p.returncode, err, _classify_error(p.returncode, p.stderr))
    if not text:
        # exit 0 but no readable message: silent failure -> explicit diagnostic.
        return CodexReply("", tid, 0, "codex ok but last-message empty/unreadable", "empty_output")
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
                   help="reasoning effort (default: codex config). low/medium for simple tasks")
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

    ap = argparse.ArgumentParser(prog="codex_bridge", description="One headless round with the Codex CLI")
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
