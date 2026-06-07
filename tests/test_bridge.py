"""Unit tests for codex_bridge (no real calls to Codex).

Hermetic: argv construction uses `_build_args` (no filesystem); tests of
ask_codex mock `tempfile.mkstemp`/`os.close` so they run even where /tmp is
not writable (e.g. a read-only sandbox).
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

from codex_bridge import bridge


def _fake_run(returncode=0, stdout="", stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


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


class TestExtractThreadId(unittest.TestCase):
    def test_from_started(self):
        out = json.dumps({"type": "thread.started", "thread_id": "abc"})
        self.assertEqual(bridge._extract_thread_id(out, None), "abc")

    def test_fallback(self):
        self.assertEqual(bridge._extract_thread_id(json.dumps({"type": "x"}), "fb"), "fb")

    def test_none_input(self):
        self.assertIsNone(bridge._extract_thread_id(None, None))


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

        def fake(args, **kw):
            seen["args"] = args
            return _fake_run()

        with _fake_temp(), mock.patch.object(bridge.subprocess, "run", side_effect=fake), \
             mock.patch.object(bridge.os, "getcwd", return_value="/here/now"), \
             mock.patch.object(bridge.Path, "read_text", return_value="ok"):
            bridge.ask_codex("p")          # no cwd -> defaults to getcwd()
        i = seen["args"].index("-C")
        self.assertEqual(seen["args"][i + 1], "/here/now")


class TestAskCodexErrorPaths(unittest.TestCase):
    def _run(self, **patch_run):
        with _fake_temp(), mock.patch.object(bridge.subprocess, "run", **patch_run):
            return bridge.ask_codex("p", "thr-1")

    def test_timeout_recovers_thread_id(self):
        ev = json.dumps({"type": "thread.started", "thread_id": "rec"}).encode()
        r = self._run(side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=1, output=ev))
        self.assertEqual(r.exit_code, 124)
        self.assertEqual(r.error_kind, "timeout")
        self.assertEqual(r.thread_id, "rec")

    def test_codex_missing(self):
        r = self._run(side_effect=FileNotFoundError("no codex"))
        self.assertEqual(r.error_kind, "codex_missing")

    def test_permission_error(self):
        r = self._run(side_effect=PermissionError(13, "denied"))
        self.assertEqual(r.exit_code, 127)

    def test_returncode_nonzero_no_text(self):
        r = self._run(return_value=_fake_run(returncode=1, stderr="no rollout found (code -32600)"))
        self.assertEqual(r.text, "")
        self.assertEqual(r.error_kind, "resume_invalid")

    def test_temp_unavailable(self):
        with mock.patch.object(bridge, "_codex_bin", return_value="codex"), \
             mock.patch.object(bridge.tempfile, "mkstemp", side_effect=OSError("no temp")):
            r = bridge.ask_codex("p")
        self.assertEqual(r.error_kind, "temp_unavailable")

    def test_prompt_via_stdin(self):
        seen = {}

        def fake(args, **kw):
            seen["input"] = kw.get("input")
            return _fake_run()

        with _fake_temp(), mock.patch.object(bridge.subprocess, "run", side_effect=fake), \
             mock.patch.object(bridge.Path, "read_text", return_value="ok"):
            bridge.ask_codex("PROMPT")
        self.assertEqual(seen["input"], "PROMPT")


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


if __name__ == "__main__":
    unittest.main()
