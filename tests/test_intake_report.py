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

"""The status rule, locations, details body and five lines of core.intake_report."""

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


class RenderDetailsTest(unittest.TestCase):
    def _render(self, **kw):
        summary = kw.pop("summary", None) or _summary("/x", errors=["agg: odd"])
        locs = {"summary_json": Path("/r/s/summary.json"), "harness_dir": Path("/r/h/c1")}
        return ir.render_details(summary, mode="agg", locations=locs, **kw)

    def test_has_five_headings_and_no_status_line(self):
        text = self._render()
        for head in ("## 自己判定", "## 変更", "## 検証", "## 未解決", "## ハーネス"):
            self.assertIn(head + "\n", text)
        self.assertFalse(text.startswith("状態:"))
        self.assertIn("/r/s/summary.json", text)
        self.assertIn("/r/h/c1", text)
        self.assertIn("chosen_tree_is_base: False", text)
        self.assertIn("- agg: odd", text)

    def test_why_is_cut_at_4000(self):
        text = self._render(summary=_summary("/x", why="A" * 5000 + "TAIL"))
        self.assertIn("A" * 4000, text)
        self.assertNotIn("A" * 4001, text)
        self.assertNotIn("TAIL", text)

    def test_changed_and_verify_are_listed(self):
        verify = [
            {"n": 1, "command": "python3 -m unittest", "code": 0},
            {"n": 2, "command": "ruff check", "code": 1},
        ]
        text = self._render(verify=verify, changed=["a.py", "b/c.py"])
        self.assertIn("- a.py\n- b/c.py", text)
        self.assertIn("- 1: `python3 -m unittest` -> 0", text)
        self.assertIn("- 2: `ruff check` -> 1", text)
        self.assertIn("pass 1/2", text)

    def test_empty_sections_say_none(self):
        text = self._render(summary=_summary("/x"))
        self.assertIn("## 変更\nなし", text)
        self.assertIn("## 未解決\nなし", text)


class FiveLinesTest(unittest.TestCase):
    def test_format(self):
        text = ir.five_lines(
            ir.DONE,
            passed=3,
            total=4,
            changed=["a.py", "b.py"],
            unresolved="x\ny",
            details_path="docs/pbi/reports/PBI-021.md",
        )
        self.assertEqual(
            text.split("\n"),
            [
                "状態: DONE",
                "結果: 受け入れ条件 pass 3/4",
                "変更: a.py, b.py",
                "未解決: x y",
                "詳細: docs/pbi/reports/PBI-021.md",
            ],
        )

    def test_empty_values(self):
        lines = ir.five_lines(
            ir.BLOCKED, passed=0, total=0, changed=[], unresolved="", details_path="d.md"
        ).split("\n")
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[2], "変更: なし")
        self.assertEqual(lines[3], "未解決: なし")


if __name__ == "__main__":
    unittest.main()
