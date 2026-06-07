"""Real integration tests against the `codex` CLI — skipped by default.

The unit tests are all mocked: if `codex exec`'s flags drift between versions,
the mocks stay green but the driver breaks. These tests pin the CLI contract the
driver relies on.

Enable: `CODEX_BRIDGE_IT=1 python -m pytest tests/test_integration.py`
(requires `codex` on PATH). Without the env var -> skipped.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

_ENABLED = os.environ.get("CODEX_BRIDGE_IT") == "1"
_HAS_CODEX = shutil.which("codex") is not None


@unittest.skipUnless(_ENABLED and _HAS_CODEX, "integration off (set CODEX_BRIDGE_IT=1 + codex on PATH)")
class TestCodexCliContract(unittest.TestCase):
    def _help(self, *args: str) -> str:
        p = subprocess.run(["codex", *args, "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, f"`codex {' '.join(args)} --help` exit {p.returncode}: {p.stderr[:200]}")
        return p.stdout + p.stderr

    def test_exec_exposes_flags(self):
        h = self._help("exec")
        for flag in ("-o", "--json", "-s", "-C", "--color"):
            self.assertIn(flag, h, f"`codex exec` no longer exposes {flag} — the round-1 driver breaks")

    def test_resume_exposes_flags(self):
        h = self._help("exec", "resume")
        for flag in ("-o", "--json", "-c"):
            self.assertIn(flag, h, f"`codex exec resume` no longer exposes {flag} — the resume driver breaks")
        self.assertIn("SESSION_ID", h)


if __name__ == "__main__":
    unittest.main()
