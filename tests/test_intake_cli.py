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

"""harness2.cli.intake: arguments, the flow around run_intake, the printed reports, the exit code.

connector_for, OpencodeAgent, WorktreeLauncher, Proposer and run_intake are fakes: no network,
no model. --repo is a fresh `git init` in a temporary directory (the argument check asks git
for the work tree's root).
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness2.benchmarks.intake import HANDLED_MARK
from harness2.cli import intake as cli
from harness2.core import intake_report
from harness2.core.spec import ChannelSpec

URL = "http://127.0.0.1:7830/mcp"


def _five(status: str, task_id: str) -> str:
    return intake_report.five_lines(
        status,
        passed=3,
        total=4,
        changed=["harness2/x.py"],
        unresolved="",
        details_path=f"docs/pbi/mcp/{task_id}.md",
    )


class IntakeCliTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        self.work = self.tmp / "jev" / "h2"
        self.client = mock.MagicMock(name="client")
        self.connector = mock.MagicMock(name="connector")
        self.connector.client = self.client
        self.launcher = mock.MagicMock(name="launcher")
        self.run_intake = mock.MagicMock(name="run_intake", return_value=[])
        self.connector_for = mock.MagicMock(return_value=self.connector)
        patches = [
            mock.patch.object(cli.intake, "connector_for", self.connector_for),
            mock.patch.object(cli, "OpencodeAgent"),
            mock.patch.object(cli, "WorktreeLauncher", return_value=self.launcher),
            mock.patch.object(cli, "Proposer"),
            mock.patch.object(cli, "run_intake", self.run_intake),
            mock.patch.object(
                cli,
                "resolve_channel_model",
                return_value=ChannelSpec("opencode", "fake/model"),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _args(self, *extra: str) -> list[str]:
        return ["--repo", str(self.repo), "--work-root", str(self.work), *extra]

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _exit_code(self, argv: list[str]) -> int:
        return self._exit_code_and_err(argv)[0]

    def _exit_code_and_err(self, argv: list[str]) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            cli.main(argv)
        return cm.exception.code, err.getvalue()

    def _take(self, *task_ids: str):
        """A run_intake that stores and marks `task_ids` as handled."""

        def fake(adapter, prop, **kwargs):
            for task_id in task_ids:
                d = adapter.root / "tasks" / task_id
                d.mkdir(parents=True, exist_ok=True)
                (d / "spec.json").write_text("{}", encoding="utf-8")
                (d / HANDLED_MARK).write_text("delivered", encoding="utf-8")
            return []

        self.run_intake.side_effect = fake

    def _report(self, task_id: str, status: str) -> str:
        text = _five(status, task_id)
        path = self.repo / "docs" / "pbi" / "mcp" / f"{task_id}.last.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        return text

    # -- argument errors ---------------------------------------------------------------

    def test_url_and_command_together_is_exit_2(self) -> None:
        argv = self._args("--mcp-url", URL, "--mcp-command", "jev-mcp --stdio")
        self.assertEqual(self._exit_code(argv), 2)
        self.connector_for.assert_not_called()

    def test_neither_url_nor_command_is_exit_2(self) -> None:
        self.assertEqual(self._exit_code(self._args()), 2)
        self.connector_for.assert_not_called()

    def test_work_root_under_repo_is_exit_2(self) -> None:
        argv = [
            "--repo",
            str(self.repo),
            "--work-root",
            str(self.repo / "sub" / "h2"),
            "--mcp-url",
            URL,
        ]
        self.assertEqual(self._exit_code(argv), 2)
        argv[3] = str(self.repo)
        self.assertEqual(self._exit_code(argv), 2)
        self.connector_for.assert_not_called()

    def test_repo_subdirectory_is_exit_2(self) -> None:
        sub = self.repo / "sub"
        sub.mkdir()
        argv = ["--repo", str(sub), "--work-root", str(self.work), "--mcp-url", URL]
        code, err = self._exit_code_and_err(argv)
        self.assertEqual(code, 2)
        self.assertIn("root of its git work tree", err)
        self.connector_for.assert_not_called()

    def test_repo_outside_a_git_work_tree_is_exit_2(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        argv = ["--repo", str(plain), "--work-root", str(self.work), "--mcp-url", URL]
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)}):
            code, err = self._exit_code_and_err(argv)
        self.assertEqual(code, 2)
        self.assertIn("not in a git work tree", err)
        self.connector_for.assert_not_called()

    def test_work_root_under_the_work_tree_root_is_exit_2(self) -> None:
        sub = self.repo / "sub"
        sub.mkdir()
        argv = ["--repo", str(sub), "--work-root", str(self.repo / "wr"), "--mcp-url", URL]
        code, err = self._exit_code_and_err(argv)
        self.assertEqual(code, 2)
        self.assertIn("--work-root", err)
        self.assertIn("inside the --repo work tree", err)
        self.connector_for.assert_not_called()

    # -- the flow ----------------------------------------------------------------------

    def test_normal_flow_runs_intake_then_cleans_up_and_closes(self) -> None:
        argv = self._args("--mcp-command", "jev-mcp --stdio", "--k", "2", "--seed", "7")
        argv += ["--tag", "t1"]
        code, _out, _err = self._run(argv)
        self.assertEqual(code, 0)
        kwargs = self.connector_for.call_args.kwargs
        self.assertEqual(kwargs["name"], "jev-mcp")
        self.assertEqual(kwargs["repo"], str(self.repo))
        self.assertIsNone(kwargs["url"])
        self.assertEqual(kwargs["command"], ["jev-mcp", "--stdio"])
        self.assertEqual(kwargs["mode"], "agg")
        self.run_intake.assert_called_once()
        run_kwargs = self.run_intake.call_args.kwargs
        self.assertEqual(run_kwargs["k"], 2)
        self.assertEqual(run_kwargs["seed"], 7)
        self.assertEqual(run_kwargs["tag"], "t1")
        self.assertEqual(run_kwargs["modes"], ("agg",))
        self.assertEqual(run_kwargs["runs_root"], self.work / "runs")
        adapter = self.run_intake.call_args.args[0]
        self.assertIs(adapter.connector, self.connector)
        self.assertIs(adapter.judge.client, self.client)
        self.launcher.cleanup.assert_called_once()
        self.client.close.assert_called_once()

    def test_cleanup_and_close_run_when_run_intake_raises(self) -> None:
        self.run_intake.side_effect = RuntimeError("boom")
        code, _out, err = self._run(self._args("--mcp-url", URL))
        self.assertEqual(code, 1)
        self.assertIn("boom", err)
        self.launcher.cleanup.assert_called_once()
        self.client.close.assert_called_once()

    def test_locate_points_at_the_summary_of_the_run(self) -> None:
        self._run(self._args("--mcp-url", URL, "--runs-root", str(self.tmp / "r")))
        locate = self.connector_for.call_args.kwargs["locate"]
        found = locate({"task_id": "PBI-900", "k": 1, "seed": None, "selections": {}})
        self.assertEqual(found["summary_json"].name, "summary.json")
        self.assertIn(str(self.tmp / "r"), str(found["summary_json"]))
        self.assertIn("PBI-900", str(found["summary_json"]))

    # -- output and exit code ----------------------------------------------------------

    def test_last_md_reports_are_printed(self) -> None:
        first = self._report("PBI-900", "DONE")
        second = self._report("PBI-901", "DONE")
        self._take("PBI-900", "PBI-901")
        code, out, _err = self._run(self._args("--mcp-url", URL))
        self.assertEqual(code, 0)
        self.assertEqual(out, first + "\n\n" + second + "\n")

    def test_missing_report_is_one_line_and_exit_1(self) -> None:
        self._take("PBI-902")
        code, out, _err = self._run(self._args("--mcp-url", URL))
        self.assertEqual(out, "PBI-902: no report written\n")
        self.assertEqual(code, 1)

    def test_task_handled_before_this_run_is_not_printed(self) -> None:
        old = self.work / "tasks" / "PBI-800"
        old.mkdir(parents=True)
        (old / "spec.json").write_text("{}", encoding="utf-8")
        (old / HANDLED_MARK).write_text("failed", encoding="utf-8")
        self._report("PBI-800", "BLOCKED")
        text = self._report("PBI-900", "DONE")
        self._take("PBI-900")
        code, out, _err = self._run(self._args("--mcp-url", URL))
        self.assertEqual(out, text + "\n")
        self.assertEqual(code, 0)

    def test_exit_code_follows_the_statuses(self) -> None:
        cases = {
            ("DONE",): 0,
            ("DONE", "DONE_WITH_CONCERNS"): 1,
            ("BLOCKED",): 1,
            ("DONE", "BLOCKED"): 1,
        }
        for n, (statuses, expected) in enumerate(cases.items()):
            with self.subTest(statuses=statuses):
                ids = [f"PBI-{n}{i:02d}" for i in range(len(statuses))]
                for task_id, status in zip(ids, statuses):
                    self._report(task_id, status)
                self._take(*ids)
                code, _out, _err = self._run(self._args("--mcp-url", URL))
                self.assertEqual(code, expected)


if __name__ == "__main__":
    unittest.main()
