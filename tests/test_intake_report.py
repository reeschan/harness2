# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The generic status rule and summary locations of core.intake_report."""

import tempfile
import unittest
from pathlib import Path

from harness2.core import intake_report as ir
from harness2.core.schedule import Namespace, TaskResult


def _summary(run_dir="", *, errors=(), status="ok", mode="agg", why="best", label="c1"):
    res = TaskResult(task_id="PBI-021", domain="intake", k=2, seed=None, status=status)
    res.base = [{"label": "base1", "run_id": "x/base1", "run_dir": "/nowhere"}]
    res.errors = list(errors)
    if run_dir is not None:
        res.modes[mode] = {"ok": True}
        res.selections[mode] = {
            "ok": True,
            "chosen_label": label,
            "why": why,
            "chosen_run_dir": str(run_dir),
            "chosen_tree_is_base": False,
        }
    return res.to_dict()


class ReportStatusTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.run_dir = self.tmp / "run"
        self.out = self.run_dir / "workspace" / "output"
        self.out.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_output_and_no_errors_is_done(self):
        (self.out / "changes.diff").write_text("diff\n")
        self.assertEqual(ir.report_status(_summary(self.run_dir), mode="agg"), ir.DONE)

    def test_nested_output_counts(self):
        (self.out / "sub").mkdir()
        (self.out / "sub" / "a.txt").write_text("a")
        self.assertEqual(ir.report_status(_summary(self.run_dir), mode="agg"), ir.DONE)

    def test_output_with_errors_is_concerns(self):
        (self.out / "changes.diff").write_text("diff\n")
        summary = _summary(self.run_dir, errors=["seq: boom"], status="partial")
        self.assertEqual(ir.report_status(summary, mode="agg"), ir.DONE_WITH_CONCERNS)

    def test_all_base_failed_is_blocked(self):
        summary = TaskResult(task_id="PBI-021", domain="intake", k=2, status="base-failed")
        summary.errors.append("no base rollout produced anything")
        self.assertEqual(ir.report_status(summary.to_dict(), mode="agg"), ir.BLOCKED)

    def test_ran_with_errors_but_no_output_is_concerns(self):
        summary = _summary(self.run_dir, errors=["agg: boom"], status="partial")
        self.assertEqual(ir.report_status(summary, mode="agg"), ir.DONE_WITH_CONCERNS)

    def test_status_values(self):
        self.assertEqual(
            (ir.DONE, ir.DONE_WITH_CONCERNS, ir.BLOCKED), ("DONE", "DONE_WITH_CONCERNS", "BLOCKED")
        )

    def test_chosen_run_without_output_is_concerns(self):
        summary = _summary(self.run_dir)
        self.assertEqual(ir.report_status(summary, mode="agg"), ir.DONE_WITH_CONCERNS)

    def test_symlinked_output_file_is_not_a_deliverable(self):
        target = self.tmp / "elsewhere.txt"
        target.write_text("x")
        (self.out / "link.txt").symlink_to(target)
        summary = _summary(self.run_dir)
        self.assertEqual(ir.report_status(summary, mode="agg"), ir.DONE_WITH_CONCERNS)

    def test_symlinked_output_dir_is_not_a_deliverable(self):
        real = self.tmp / "real"
        real.mkdir()
        (real / "a.txt").write_text("a")
        other = self.tmp / "run2"
        (other / "workspace").mkdir(parents=True)
        (other / "workspace" / "output").symlink_to(real, target_is_directory=True)
        summary = _summary(other)
        self.assertEqual(ir.report_status(summary, mode="agg"), ir.DONE_WITH_CONCERNS)

    def test_missing_mode_selection(self):
        (self.out / "changes.diff").write_text("diff\n")
        summary = _summary(self.run_dir, mode="agg")
        self.assertEqual(ir.report_status(summary, mode="seq"), ir.DONE_WITH_CONCERNS)
        failed = dict(summary, selections={}, status="base-failed")
        self.assertEqual(ir.report_status(failed, mode="seq"), ir.BLOCKED)

    def test_empty_chosen_run_dir(self):
        summary = _summary("")
        self.assertEqual(ir.report_status(summary, mode="agg"), ir.DONE_WITH_CONCERNS)


class SummaryLocationsTest(unittest.TestCase):
    def test_matches_run_task_namespaces(self):
        root = Path("/runs")
        for seed, tag in ((None, ""), (3, "t1")):
            summary = _summary("/x", mode="seq", label="c2")
            summary["seed"] = seed
            got = ir.summary_locations(
                root, summary, bench="intake", substrate="oc", mode="seq", tag=tag
            )
            base = Namespace("intake", "oc", "agg", 2, seed, tag).task_dir(root, "PBI-021")
            mode_dir = Namespace("intake", "oc", "seq", 2, seed, tag).task_dir(root, "PBI-021")
            self.assertEqual(got["summary_json"], base / "summary.json")
            self.assertEqual(got["harness_dir"], mode_dir / "harnesses" / "c2")

    def test_no_chosen_label(self):
        summary = _summary("/x", label="")
        got = ir.summary_locations(Path("/r"), summary, bench="b", substrate="oc", mode="agg")
        self.assertIsNone(got["harness_dir"])
        none = ir.summary_locations(Path("/r"), summary, bench="b", substrate="oc", mode="seq")
        self.assertIsNone(none["harness_dir"])


if __name__ == "__main__":
    unittest.main()
