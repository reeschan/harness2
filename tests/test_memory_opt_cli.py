"""--memory-opt KEY=VALUE in the sweep CLI: parsing, the check before the run, the warning."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harness2 import config
from harness2.cli import _common as C
from harness2.cli import run_sweep
from harness2.core import memory, schedule


def _factory(root, domain, surface, url=None, timeout=None):
    raise AssertionError("the CLI test never builds a connector")


class MemoryOptCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        runs = Path(self._tmp.name)
        adapter = SimpleNamespace(
            spec=SimpleNamespace(name="bench", channels={"proposer": None}),
            substrate=SimpleNamespace(name="oc"),
        )
        env = {k: v for k, v in os.environ.items() if k != memory.ENV_VAR}
        self.sweep = MagicMock(return_value={"domains": [{"domain": "d", "done": 1}]})
        self.stream = MagicMock(return_value=({"d": ["t"]}, [("d", "t")]))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(patch.object(config, "MEMORY_SCOPE", ""))
        stack.enter_context(patch.object(config, "MEMORY_RECALL", False))
        stack.enter_context(patch.object(C, "make_adapter", return_value=adapter))
        stack.enter_context(patch.object(C, "runs_root_for", return_value=runs))
        stack.enter_context(patch.object(C, "library_root_for", return_value=runs / "lib"))
        stack.enter_context(patch.object(C, "summary_namespace", return_value="ns"))
        stack.enter_context(patch.object(run_sweep, "_stream_for", self.stream))
        stack.enter_context(patch.object(run_sweep, "_check_identity"))
        stack.enter_context(patch.object(run_sweep, "Proposer"))
        stack.enter_context(patch("harness2.preflight.require_ready"))
        stack.enter_context(patch.object(schedule, "run_sweep", self.sweep))
        stack.enter_context(patch.object(memory, "resolve_factory", return_value=_factory))

    def _run(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = run_sweep.main(["--bench", "jb", *extra])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_repeated_options_reach_run_sweep_as_strings(self):
        with patch.object(memory, "check_options") as check:
            code, _, _ = self._run("--memory-opt", "a=1", "--memory-opt", "b=x=y")
        self.assertEqual(code, 0)
        check.assert_called_once_with(memory.DEFAULT, {"a": "1", "b": "x=y"})
        self.assertEqual(self.sweep.call_args.kwargs["memory_options"], {"a": "1", "b": "x=y"})

    def test_later_repeat_of_a_key_wins(self):
        with patch.object(memory, "check_options"):
            self._run("--memory-opt", "a=1", "--memory-opt", "a=2")
        self.assertEqual(self.sweep.call_args.kwargs["memory_options"], {"a": "2"})

    def test_malformed_option_is_an_argument_error(self):
        code, _, err = self._run("--memory-opt", "nokey")
        self.assertEqual(code, 2)
        self.assertIn("--memory-opt must be KEY=VALUE", err)
        code, _, err = self._run("--memory-opt", "=x")
        self.assertEqual(code, 2)
        self.assertIn("--memory-opt has an empty key", err)
        self.sweep.assert_not_called()

    def test_malformed_option_error_hides_the_value_and_names_its_position(self):
        cases = (
            ("=topsecret", "--memory-opt has an empty key"),
            ("topsecret", "--memory-opt must be KEY=VALUE"),
        )
        for bad, message in cases:
            with self.subTest(bad=bad):
                code, out, err = self._run("--memory-opt", "a=1", "--memory-opt", bad)
                self.assertEqual(code, 2)
                self.assertIn(message, err)
                self.assertIn("number 2", err)
                self.assertNotIn("topsecret", err)
                self.assertNotIn("topsecret", out)
        self.sweep.assert_not_called()

    def test_option_the_factory_cannot_take_stops_before_any_task(self):
        code, _, err = self._run("--memory-opt", "bogus=1")
        self.assertEqual(code, 2)
        self.assertIn("bogus", err)
        self.sweep.assert_not_called()
        self.stream.assert_not_called()

    def test_reserved_option_is_an_argument_error(self):
        code, _, err = self._run("--memory-opt", "root=/x")
        self.assertEqual(code, 2)
        self.assertIn("root", err)
        self.sweep.assert_not_called()

    def test_dry_run_checks_options_too(self):
        code, _, err = self._run("--dry-run", "--memory-opt", "bogus=1")
        self.assertEqual(code, 2)
        self.assertIn("bogus", err)

    def test_warning_lists_keys_not_values(self):
        code, out, _ = self._run("--memory-opt", "url=http://secret", "--memory-opt", "timeout=5")
        self.assertEqual(code, 0)
        warning = next(line for line in out.splitlines() if line.startswith("warning:"))
        self.assertIn("options: timeout, url", warning)
        self.assertNotIn("secret", out)
        self.assertNotIn("http://", out)

    def test_without_option_nothing_changes(self):
        with patch.object(memory, "check_options") as check:
            code, out, _ = self._run()
        self.assertEqual(code, 0)
        check.assert_not_called()
        self.assertNotIn("warning:", out)
        self.assertFalse(self.sweep.call_args.kwargs["memory_options"])


class MemoryWarningOptionsTest(unittest.TestCase):
    def test_default_with_no_or_empty_options_has_no_warning(self):
        self.assertIsNone(run_sweep._memory_warning("file", "", options=None))
        self.assertIsNone(run_sweep._memory_warning("file", "", options={}))

    def test_keys_sorted_and_values_hidden(self):
        line = run_sweep._memory_warning("file", "", options={"url": "s3cret", "timeout": "5"})
        self.assertIn("options: timeout, url", line)
        self.assertNotIn("s3cret", line)
        self.assertNotIn("\n", line)


if __name__ == "__main__":
    unittest.main()
