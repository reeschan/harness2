"""HARNESS2_MEMORY_SCOPE in the CLI: the library root and the sweep's start-up warning."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness2 import config
from harness2.cli import _common as C
from harness2.cli import run_sweep


def _adapter(runs_root: Path, name: str = "bench", substrate: str = "oc"):
    return SimpleNamespace(
        spec=SimpleNamespace(runs_root=runs_root, name=name),
        substrate=SimpleNamespace(name=substrate),
    )


class LibraryRootScopeTest(unittest.TestCase):
    def setUp(self):
        self.runs = Path("/tmp/h2-runs")
        self.adapter = _adapter(self.runs)

    def test_empty_scope_keeps_per_run_root(self):
        lib = self.runs / "library" / "bench"
        for scope in ("", None):
            with self.subTest(scope=scope), patch.object(config, "MEMORY_SCOPE", ""):
                self.assertEqual(
                    C.library_root_for(self.adapter, 2, seed=None, scope=scope), lib / "k2-oc"
                )
                self.assertEqual(
                    C.library_root_for(self.adapter, 1, seed=7, scope=scope), lib / "k1-oc-s7"
                )
                self.assertEqual(
                    C.library_root_for(self.adapter, 3, seed=7, tag="t", scope=scope),
                    lib / "k3-oc-s7-t",
                )

    def test_scope_shares_root_across_k_and_seed(self):
        want = self.runs / "library" / "bench" / "scope-team1"
        for k, seed in ((1, None), (2, 7), (4, 0)):
            with self.subTest(k=k, seed=seed):
                self.assertEqual(
                    C.library_root_for(self.adapter, k, seed=seed, scope="team1"), want
                )
        cx = _adapter(self.runs, substrate="cx")
        self.assertEqual(C.library_root_for(cx, 1, seed=3, scope="team1"), want)

    def test_scope_none_reads_config(self):
        with patch.object(config, "MEMORY_SCOPE", "team1"):
            self.assertEqual(
                C.library_root_for(self.adapter, 1, seed=None),
                self.runs / "library" / "bench" / "scope-team1",
            )

    def test_invalid_scope_raises(self):
        for scope in ("a/b", "-x", "s1"):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                C.library_root_for(self.adapter, 1, seed=None, scope=scope)


class MemoryWarningTest(unittest.TestCase):
    def test_default_has_no_warning(self):
        self.assertIsNone(run_sweep._memory_warning("file", ""))

    def test_non_default_warns_in_one_line(self):
        for connector, scope in (("mod:Factory", ""), ("file", "team1"), ("x:Y", "team1")):
            with self.subTest(connector=connector, scope=scope):
                line = run_sweep._memory_warning(connector, scope)
                self.assertIsNotNone(line)
                self.assertTrue(line.startswith("warning:"))
                self.assertNotIn("\n", line)
                self.assertIn(connector, line)
                if scope:
                    self.assertIn(scope, line)


if __name__ == "__main__":
    unittest.main()
