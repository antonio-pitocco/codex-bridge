"""Unit tests for codex_bridge (no real calls to the reviewer CLI).

Hermetic: argv construction uses `_build_args` (no filesystem); tests of
ask_codex mock `tempfile.mkstemp`/`os.close` so they run even where /tmp is
not writable (e.g. a read-only sandbox).

The only non-hermetic test is `TestProcessTreeTimeout`, which spawns real
short-lived `sys.executable` processes: the process-group kill cannot be
proven with a mock, and that is exactly the property worth proving.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_bridge import bridge


def _fake_run(returncode=0, stdout="", stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


#: A MINIMAL but INTACT `--json` stream, in the shape verified live.
STREAM_OK = "\n".join([
    '{"type": "thread.started", "thread_id": "t-1"}',
    '{"type": "item.completed", "item": {"id": "i0",'
    ' "type": "agent_message", "text": "ok"}}',
    '{"type": "turn.completed"}',
])


@contextlib.contextmanager
def _fake_temp():
    with mock.patch.object(bridge.tempfile, "mkstemp", return_value=(-1, "/tmp/fake_last")), \
         mock.patch.object(bridge.os, "close"), \
         mock.patch.object(bridge, "_safe_unlink"), \
         mock.patch.object(bridge, "_codex_bin", return_value="codex"):
        yield


class TestParseVerdict(unittest.TestCase):
    def test_agree_last_line(self):
        self.assertEqual(bridge.parse_verdict("bla\nVERDICT: AGREE"), "AGREE")

    def test_object_with_trailing_blank_lines(self):
        self.assertEqual(bridge.parse_verdict("x\nVERDICT: OBJECT\n\n  \n"), "OBJECT")

    def test_text_after_verdict_is_malformed(self):
        # the verdict is NOT the last line -> fail-safe
        self.assertEqual(bridge.parse_verdict("t\nVERDICT: AGREE\nnote after"), "MALFORMED")

    def test_two_verdicts_with_tail_malformed(self):
        self.assertEqual(bridge.parse_verdict("VERDICT: OBJECT\n...\nVERDICT: AGREE\ntail"), "MALFORMED")

    def test_absent_is_malformed(self):
        self.assertEqual(bridge.parse_verdict("no verdict here"), "MALFORMED")

    def test_caveat_is_malformed(self):
        self.assertEqual(bridge.parse_verdict("VERDICT: AGREE with caveats"), "MALFORMED")

    def test_case_insensitive_and_spaces(self):
        self.assertEqual(bridge.parse_verdict("  verdict:   agree  "), "AGREE")

    def test_empty(self):
        self.assertEqual(bridge.parse_verdict(""), "MALFORMED")


class TestConsensus(unittest.TestCase):
    def test_trivial_closes_in_1_round(self):
        self.assertTrue(bridge.consensus_reached(1, "trivial", "AGREE", True))

    def test_critical_needs_3(self):
        self.assertFalse(bridge.consensus_reached(2, "critical", "AGREE", True))
        self.assertTrue(bridge.consensus_reached(3, "critical", "AGREE", True))

    def test_unknown_complexity_fail_safe_critical(self):
        self.assertFalse(bridge.consensus_reached(2, "boh", "AGREE", True))
        self.assertTrue(bridge.consensus_reached(3, "boh", "AGREE", True))

    def test_case_insensitive(self):
        self.assertFalse(bridge.consensus_reached(2, "CRITICAL", "AGREE", True))

    def test_object_never_consensus(self):
        self.assertFalse(bridge.consensus_reached(9, "trivial", "OBJECT", True))

    def test_not_satisfied_never_consensus(self):
        self.assertFalse(bridge.consensus_reached(9, "trivial", "AGREE", False))

    def test_malformed_never_consensus(self):
        self.assertFalse(bridge.consensus_reached(9, "trivial", "MALFORMED", True))


class TestShouldContinuePastCap(unittest.TestCase):
    """The ceiling is an attention threshold, not a ban."""

    def test_below_the_cap_always_continues(self):
        self.assertTrue(bridge.should_continue_past_cap(1, "critical", 0, False))
        self.assertTrue(bridge.should_continue_past_cap(2, "standard", 0, False))

    def test_at_the_cap_continues_while_defects_are_still_found(self):
        # 5 rounds done on critical, the last one still produced accepted defects
        self.assertTrue(bridge.should_continue_past_cap(5, "critical", 2, False))
        self.assertTrue(bridge.should_continue_past_cap(9, "critical", 1, False))

    def test_at_the_cap_stops_when_the_last_round_produced_nothing(self):
        self.assertFalse(bridge.should_continue_past_cap(5, "critical", 0, False))
        self.assertFalse(bridge.should_continue_past_cap(3, "standard", 0, False))

    def test_disagreement_on_merit_always_stops(self):
        """More rounds do not dissolve a judgement call: a human does."""
        self.assertFalse(bridge.should_continue_past_cap(1, "critical", 5, True))

    def test_unknown_complexity_uses_the_highest_cap(self):
        self.assertTrue(bridge.should_continue_past_cap(4, "whatever", 0, False))
        self.assertFalse(bridge.should_continue_past_cap(5, "whatever", 0, False))


class TestExtractThreadId(unittest.TestCase):
    def test_from_started(self):
        out = json.dumps({"type": "thread.started", "thread_id": "abc"})
        self.assertEqual(bridge._extract_thread_id(out, None), "abc")

    def test_fallback(self):
        self.assertEqual(bridge._extract_thread_id(json.dumps({"type": "x"}), "fb"), "fb")

    def test_none_input(self):
        self.assertIsNone(bridge._extract_thread_id(None, None))


class TestExtractAgentMessages(unittest.TestCase):
    """The driver used to hand back the WRAP-UP instead of the audit.

    A reviewer often closes a turn with a few lines of summary, and
    `--output-last-message` writes only those: a real review round returned
    713 and 135 characters while the actual messages of the turn were 14,427
    and 25,931. The audit was being discarded and the summary kept.
    Event shape reproduced from a real stream, not inferred.
    """

    STREAM = "\n".join([
        '{"type": "thread.started", "thread_id": "t-1"}',
        '{"type": "turn.started"}',
        '{"type": "item.completed", "item": {"id": "item_0",'
        ' "type": "agent_message", "text": "LONG AUDIT\\nlines and counterexamples"}}',
        'non-JSON noise that must not break the parser',
        '{"type": "item.completed", "item": {"id": "item_1",'
        ' "type": "reasoning", "text": "reasoning, not a message"}}',
        '{"type": "item.completed", "item": {"id": "item_2",'
        ' "type": "agent_message", "text": "wrap-up\\n\\nVERDICT: OBJECT"}}',
        '{"type": "turn.completed"}',
    ])

    def test_takes_every_message_not_only_the_last(self):
        messages, intact = bridge._extract_agent_messages(self.STREAM)
        self.assertEqual(len(messages), 2)
        self.assertIn("LONG AUDIT", messages[0])
        self.assertNotIn("reasoning", " ".join(messages))
        self.assertTrue(intact)

    def test_verdict_is_still_the_last_line_after_joining(self):
        text = "\n\n".join(bridge._extract_agent_messages(self.STREAM)[0])
        self.assertIn("LONG AUDIT", text)
        self.assertEqual(bridge.parse_verdict(text), "OBJECT")

    def test_empty_or_unreadable_stream(self):
        for stream in ("", None, '{"type": "turn.started"}'):
            self.assertEqual(bridge._extract_agent_messages(stream)[0], [])
        messages, intact = bridge._extract_agent_messages("{not json")
        self.assertEqual(messages, [])
        self.assertFalse(intact)

    def test_item_updated_does_not_add_up_to_the_completion(self):
        """Matching on `item.type` alone also accepted partials: an
        `item.updated` duplicated the text of the `item.completed` that
        followed, and a partial left alone suppressed the fallback because
        `text` was not empty."""
        stream = "\n".join([
            '{"type": "item.updated", "item": {"id": "a",'
            ' "type": "agent_message", "text": "PARTIAL"}}',
            '{"type": "item.completed", "item": {"id": "a",'
            ' "type": "agent_message", "text": "FINAL"}}',
            '{"type": "turn.completed"}',
        ])
        self.assertEqual(bridge._extract_agent_messages(stream)[0], ["FINAL"])

    def test_stream_without_turn_completed_is_not_intact(self):
        """A truncated stream must not be presented as a complete audit."""
        only_partial = ('{"type": "item.updated", "item": {"id": "a",'
                        ' "type": "agent_message", "text": "PARTIAL"}}')
        messages, intact = bridge._extract_agent_messages(only_partial)
        self.assertEqual(messages, [])
        self.assertFalse(intact)


class TestBuildArgs(unittest.TestCase):
    def _args(self, thread_id=None, *, sandbox="read-only", model=None, effort=None):
        return bridge._build_args("codex", thread_id, sandbox, "/repo", model, effort, "/tmp/last")

    def test_round1_has_cwd_and_sandbox(self):
        a = self._args(None)
        self.assertEqual(a[1], "exec")
        self.assertIn("-C", a)
        self.assertIn("-s", a)
        self.assertNotIn("resume", a)
        self.assertEqual(a[-1], "-")

    def test_resume_no_cwd_no_sandbox_flag_but_reasserts(self):
        a = self._args("t-42")
        self.assertIn("resume", a)
        self.assertNotIn("-C", a)
        self.assertNotIn("-s", a)
        self.assertIn('sandbox_mode="read-only"', a)

    def test_effort(self):
        self.assertIn('model_reasoning_effort="low"', self._args(None, effort="low"))


class TestCwdDefault(unittest.TestCase):
    def test_cwd_none_uses_getcwd(self):
        # domain-agnostic default: cwd=None -> the round-1 argv carries os.getcwd()
        seen = {}

        def fake(args, prompt, timeout):
            seen["args"] = args
            return _fake_run(stdout=STREAM_OK)

        with _fake_temp(), mock.patch.object(bridge, "_run_codex_process", side_effect=fake), \
             mock.patch.object(bridge.os, "getcwd", return_value="/here/now"):
            bridge.ask_codex("p")          # no cwd -> defaults to getcwd()
        i = seen["args"].index("-C")
        self.assertEqual(seen["args"][i + 1], "/here/now")


class TestAskCodexWiring(unittest.TestCase):
    def test_prompt_via_stdin(self):
        seen = {}

        def fake(args, prompt, timeout):
            seen["input"] = prompt
            seen["timeout"] = timeout
            return _fake_run(stdout=STREAM_OK)

        with _fake_temp(), mock.patch.object(bridge, "_run_codex_process", side_effect=fake):
            r = bridge.ask_codex("PROMPT")
        self.assertEqual(seen["input"], "PROMPT")
        self.assertTrue(r.ok)
        self.assertEqual(r.text, "ok")

    def test_without_the_json_stream_the_round_is_NOT_valid(self):
        """Falling back to `--output-last-message` alone is exactly the historic
        defect — the loop received the wrap-up believing it was the audit. The
        fallback survives as a diagnostic but is NOT a round: `ok` is false, the
        verdict is not computed, and the command exits non-zero."""
        with _fake_temp(), \
             mock.patch.object(bridge, "_run_codex_process",
                               return_value=_fake_run(stdout="")), \
             mock.patch.object(bridge.Path, "read_text",
                               return_value="wrap-up\n\nVERDICT: AGREE"):
            r = bridge.ask_codex("PROMPT")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "degraded_last_only")
        self.assertIn("wrap-up", r.text, "the text stays for diagnostics")


class TestAskCodexErrorPaths(unittest.TestCase):
    def _run(self, **patch_run):
        with _fake_temp(), mock.patch.object(bridge, "_run_codex_process", **patch_run):
            return bridge.ask_codex("p", "thr-1")

    def test_timeout_recovers_thread_id(self):
        ev = json.dumps({"type": "thread.started", "thread_id": "rec"}).encode()
        r = self._run(side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=1, output=ev))
        self.assertEqual(r.exit_code, 124)
        self.assertEqual(r.error_kind, "timeout")
        self.assertEqual(r.thread_id, "rec")

    def test_codex_missing(self):
        r = self._run(side_effect=FileNotFoundError("binary not found"))
        self.assertEqual(r.error_kind, "codex_missing")

    def test_permission_error(self):
        r = self._run(side_effect=PermissionError(13, "denied"))
        self.assertEqual(r.exit_code, 127)

    def test_returncode_nonzero_no_text(self):
        r = self._run(return_value=_fake_run(returncode=1, stderr="no rollout found (code -32600)"))
        self.assertEqual(r.text, "")
        self.assertEqual(r.error_kind, "resume_invalid")

    def test_exit_zero_but_silent_is_diagnosed(self):
        with _fake_temp(), \
             mock.patch.object(bridge, "_run_codex_process",
                               return_value=_fake_run(stdout='{"type": "turn.completed"}')), \
             mock.patch.object(bridge.Path, "read_text", return_value=""):
            r = bridge.ask_codex("p")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "empty_output")

    def test_temp_unavailable(self):
        with mock.patch.object(bridge, "_codex_bin", return_value="codex"), \
             mock.patch.object(bridge.tempfile, "mkstemp", side_effect=OSError("no temp")):
            r = bridge.ask_codex("p")
        self.assertEqual(r.error_kind, "temp_unavailable")


class TestProcessTreeTimeout(unittest.TestCase):
    """`subprocess.run(timeout=...)` does not kill grandchildren.

    A child that ignores TERM keeps the pipes open, so `communicate()` blocks
    long past the deadline: the timeout is nominal, not real. This test spawns
    a parent that forks a TERM-ignoring child and asserts that neither survives.
    """

    def test_timeout_kills_the_child_too_and_leaves_no_orphan(self):
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / "orphan-survived"
            grandchild = (
                "import pathlib,signal,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "time.sleep(0.6);"
                f"pathlib.Path({str(sentinel)!r}).write_text('alive')"
            )
            parent = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]);"
                "time.sleep(60)"
            )
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                bridge._run_codex_process([sys.executable, "-c", parent], "", timeout=0.1)
            self.assertLess(time.monotonic() - started, 2.5)
            # If only the parent were killed, the grandchild would write this
            # file after 0.6s. The whole group must disappear instead.
            time.sleep(0.8)
            self.assertFalse(sentinel.exists(), "grandchild survived the timeout")


class TestCodexReply(unittest.TestCase):
    def test_ok_property(self):
        self.assertTrue(bridge.CodexReply("text", "t", 0).ok)
        self.assertFalse(bridge.CodexReply("", "t", 0).ok)
        self.assertFalse(bridge.CodexReply("text", "t", 1).ok)

    def test_degraded_round_is_not_ok(self):
        self.assertFalse(
            bridge.CodexReply("text", "t", 0, error_kind="degraded_last_only").ok)


class TestValidation(unittest.TestCase):
    def test_bad_sandbox(self):
        with self.assertRaises(ValueError):
            bridge.ask_codex("p", sandbox="yolo")

    def test_bad_effort(self):
        with self.assertRaises(ValueError):
            bridge.ask_codex("p", effort="turbo")


class TestMainCli(unittest.TestCase):
    def _main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bridge.main(argv)
        return code, buf.getvalue()

    def test_ask_emits_verdict(self):
        reply = bridge.CodexReply("ok\nVERDICT: AGREE", "t1", 0)
        with mock.patch.object(bridge, "ask_codex", return_value=reply):
            code, out = self._main(["ask", "--prompt", "hi"])
        d = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(d["verdict"], "AGREE")

    def test_degraded_round_exits_nonzero_and_has_no_verdict(self):
        reply = bridge.CodexReply("wrap-up\nVERDICT: AGREE", "t1", 0,
                                  error_kind="degraded_last_only")
        with mock.patch.object(bridge, "ask_codex", return_value=reply):
            code, out = self._main(["verify", "--prompt", "hi"])
        d = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(d["ok"])
        self.assertIsNone(d["verdict"], "a degraded round must not carry a verdict")

    def test_verify_forces_read_only(self):
        seen = {}

        def fake(prompt, thread, **kw):
            seen.update(kw)
            return bridge.CodexReply("ok", "t", 0)

        with mock.patch.object(bridge, "ask_codex", side_effect=fake):
            self._main(["verify", "--prompt", "hi"])
        self.assertEqual(seen.get("sandbox"), "read-only")

    def test_verify_rejects_sandbox(self):
        with self.assertRaises(SystemExit):
            self._main(["verify", "--prompt", "hi", "--sandbox", "workspace-write"])

    def test_missing_prompt_file_is_a_clean_error(self):
        with self.assertRaises(SystemExit):
            self._main(["ask", "--prompt-file", "/nonexistent/prompt.txt"])

    def test_empty_prompt_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._main(["ask", "--prompt", "   "])


if __name__ == "__main__":
    unittest.main()
