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

"""harness2.cli.intake: arguments, the connector's context keywords, the optional capabilities,
the printed reports and the exit code.

A fake connector comes in through --connector `<this module>:<factory>`. OpencodeAgent,
WorktreeLauncher, Proposer and run_intake are mocks: no network, no model. --repo is a fresh
`git init` in a temporary directory (the argument check asks git for the work tree's root).
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

from harness2.cli import intake as cli
from harness2.core import models
from harness2.core.intake import WorkspacePolicy
from harness2.core.spec import ChannelSpec

_GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
)

# What the factories below hand out and what they were called with (set by each test).
_STATE: dict = {}


class FakeConnector:
    """The four required methods; optional capabilities are set as attributes by the test."""

    def __init__(self) -> None:
        self.polls = 0
        self.delivered: list[str] = []
        self.failed: list[str] = []

    def poll(self):
        self.polls += 1
        return []

    def fetch_files(self, raw, dest) -> None:
        return None

    def deliver(self, task_id, deliverable_dir, summary) -> None:
        self.delivered.append(task_id)

    def fail(self, task_id, reason) -> None:
        self.failed.append(task_id)


class ClosingConnector(FakeConnector):
    """Reads its reports over a connection that close lets go of; records the call order."""

    def __init__(self, fail_report: str | None = None) -> None:
        super().__init__()
        self.closed = False
        self.calls: list[str] = []
        self._fail_report = fail_report

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True

    def report(self, task_id):
        self.calls.append(f"report {task_id}")
        if self.closed:
            raise ConnectionError("connection closed")
        if task_id == self._fail_report:
            raise ConnectionError("server went away")
        return f"report of {task_id}"


def repo_timeout_factory(repo, timeout):
    _STATE["kwargs"] = {"repo": repo, "timeout": timeout}
    return _STATE["connector"]


def kwargs_factory(**kwargs):
    _STATE["kwargs"] = kwargs
    return _STATE["connector"]


def not_a_connector(**kwargs):
    return object()


def needs_url_factory(url):
    _STATE["kwargs"] = {"url": url}
    return _STATE["connector"]


class FakeModelConnector:
    def __init__(self) -> None:
        self.routes_calls: list[tuple[str, str, str]] = []
        self.closed = False

    def routes(self, role, *, task_id, substrate):
        self.routes_calls.append((role, task_id, substrate))
        if role == models.Role.IMPROVER:
            return []
        return [models.ModelChoice(substrate, "fake/model")]

    def close(self) -> None:
        self.closed = True


class ClaudeCliImproverConnector(FakeModelConnector):
    """No run_session: its improver candidate names one of our channels, claude_cli."""

    def routes(self, role, *, task_id, substrate):
        if role == models.Role.IMPROVER:
            return [models.ModelChoice("claude_cli", "opus", "low")]
        return super().routes(role, task_id=task_id, substrate=substrate)


class LooseSessionConnector(FakeModelConnector):
    """run_session with a fence no runner of which meets the condition (it may run commands)."""

    def run_session(self, role, request):
        return models.SessionResult(text="x", ok=True)

    def fences(self):
        return {
            name: models.FenceDeclaration(
                capabilities=frozenset({"read", "write", "commands"}),
                workdir_only=True,
                asks_human=False,
                harness_credentials=False,
                runner=name,
            )
            for name in ("claude", "opencode", "anything")
        }


def claude_cli_improver_factory(**kwargs):
    return ClaudeCliImproverConnector()


def loose_session_factory(**kwargs):
    return LooseSessionConnector()


def model_factory(**kwargs):
    _STATE["model_kwargs"] = kwargs
    return _STATE["model_connector"]


def model_url_factory(url):
    _STATE["model_kwargs"] = {"url": url}
    return _STATE["model_connector"]


def model_not_a_connector(**kwargs):
    _STATE["model_kwargs"] = kwargs
    return object()


def model_close_raises(**kwargs):
    _STATE["model_kwargs"] = kwargs
    connector = _STATE["model_connector"]

    def close():
        connector.closed = True
        raise ConnectionError("model close failed")

    connector.close = close
    return connector


def _ref(attr: str) -> str:
    return f"{__name__}:{attr}"


class IntakeCliTest(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in _GIT_LOCATION_VARS:
            os.environ.pop(var, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        self.work = self.tmp / "out" / "h2"
        self.connector = FakeConnector()
        self.model_connector = FakeModelConnector()
        _STATE.clear()
        _STATE["connector"] = self.connector
        _STATE["model_connector"] = self.model_connector
        self.addCleanup(_STATE.clear)
        self.launcher = mock.MagicMock(name="launcher")
        self.launcher_cls = mock.MagicMock(return_value=self.launcher)
        self.run_intake = mock.MagicMock(name="run_intake", return_value=[])
        patches = [
            mock.patch.object(cli, "OpencodeAgent"),
            mock.patch.object(cli, "WorktreeLauncher", self.launcher_cls),
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

    def _args(self, *extra: str, factory: str = "kwargs_factory") -> list[str]:
        return [
            "--connector",
            _ref(factory),
            "--repo",
            str(self.repo),
            "--work-root",
            str(self.work),
            *extra,
        ]

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _exit_code_and_err(self, argv: list[str]) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            cli.main(argv)
        return cm.exception.code, err.getvalue()

    def _done_summary(self, task_id: str) -> dict:
        run_dir = self.tmp / "rd" / task_id
        out = run_dir / "workspace" / "output"
        out.mkdir(parents=True)
        (out / "result.txt").write_text("x", encoding="utf-8")
        return {"task_id": task_id, "selections": {"agg": {"chosen_run_dir": str(run_dir)}}}

    def _outcomes(self, *outcomes: tuple[str, str], then: Exception | None = None):
        """A run_intake that delivers (DONE) or fails (BLOCKED) each task, then maybe raises."""

        def fake(adapter, prop, **kwargs):
            for task_id, status in outcomes:
                if status == "DONE":
                    summary = self._done_summary(task_id)
                    adapter.connector.deliver(task_id, self.tmp / "d" / task_id, summary)
                else:
                    adapter.connector.fail(task_id, "no")
            if then is not None:
                raise then
            return []

        self.run_intake.side_effect = fake

    # -- argument errors ---------------------------------------------------------------

    def test_connector_opt_without_equals_is_exit_2(self) -> None:
        code, err = self._exit_code_and_err(self._args("--connector-opt", "url"))
        self.assertEqual(code, 2)
        self.assertIn("KEY=VALUE", err)
        self.assertEqual(self.connector.polls, 0)
        self.run_intake.assert_not_called()

    def test_repo_subdirectory_is_exit_2(self) -> None:
        sub = self.repo / "sub"
        sub.mkdir()
        argv = ["--connector", _ref("kwargs_factory"), "--repo", str(sub)]
        argv += ["--work-root", str(self.work)]
        code, err = self._exit_code_and_err(argv)
        self.assertEqual(code, 2)
        self.assertIn("root of its git work tree", err)
        self.assertNotIn("kwargs", _STATE)
        self.assertEqual(self.connector.polls, 0)

    def test_repo_outside_a_git_work_tree_is_exit_2(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        argv = ["--connector", _ref("kwargs_factory"), "--repo", str(plain)]
        argv += ["--work-root", str(self.work)]
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)}):
            code, err = self._exit_code_and_err(argv)
        self.assertEqual(code, 2)
        self.assertIn("not in a git work tree", err)
        self.assertEqual(self.connector.polls, 0)

    def test_repo_check_ignores_a_git_dir_in_the_environment(self) -> None:
        other = self.tmp / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True)
        located = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}
        sub = self.repo / "sub"
        sub.mkdir()
        sub_argv = ["--connector", _ref("kwargs_factory"), "--repo", str(sub)]
        sub_argv += ["--work-root", str(self.work)]
        with mock.patch.dict(os.environ, located):
            code, _out, _err = self._run(self._args())
            sub_code, err = self._exit_code_and_err(sub_argv)
        self.assertEqual(code, 0)
        self.assertEqual(self.launcher_cls.call_args.args[0], self.repo)
        self.assertEqual(sub_code, 2)
        self.assertIn("root of its git work tree", err)

    def test_work_root_under_the_work_tree_is_exit_2(self) -> None:
        for work_root in (self.repo / "sub" / "h2", self.repo):
            with self.subTest(work_root=work_root):
                argv = ["--connector", _ref("kwargs_factory"), "--repo", str(self.repo)]
                argv += ["--work-root", str(work_root)]
                code, err = self._exit_code_and_err(argv)
                self.assertEqual(code, 2)
                self.assertIn("inside the --repo work tree", err)
        self.assertEqual(self.connector.polls, 0)
        self.run_intake.assert_not_called()

    def test_factory_returning_a_non_connector_is_exit_2(self) -> None:
        code, err = self._exit_code_and_err(self._args(factory="not_a_connector"))
        self.assertEqual(code, 2)
        self.assertIn("not a TaskConnector", err)
        self.assertEqual(self.connector.polls, 0)
        self.run_intake.assert_not_called()

    def test_factory_missing_a_required_keyword_is_exit_2_without_traceback(self) -> None:
        code, err = self._exit_code_and_err(self._args(factory="needs_url_factory"))
        self.assertEqual(code, 2)
        self.assertIn(_ref("needs_url_factory"), err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("kwargs", _STATE)
        self.run_intake.assert_not_called()

    def test_local_connector_without_root_is_exit_2(self) -> None:
        argv = ["--connector", "local", "--repo", str(self.repo), "--work-root", str(self.work)]
        code, err = self._exit_code_and_err(argv)
        self.assertEqual(code, 2)
        self.assertIn("'local'", err)
        self.assertNotIn("Traceback", err)
        self.run_intake.assert_not_called()

    def test_capabilities_type_error_is_exit_2_and_closes_the_connector(self) -> None:
        self.connector.close = mock.MagicMock()
        self.connector.scrub_env = 5
        code, err = self._exit_code_and_err(self._args())
        self.assertEqual(code, 2)
        self.assertIn("scrub_env", err)
        self.connector.close.assert_called_once()
        self.run_intake.assert_not_called()

    def test_capabilities_error_is_not_hidden_by_a_failing_close(self) -> None:
        self.connector.close = mock.MagicMock(side_effect=RuntimeError("close failed"))
        self.connector.scrub_env = 5
        code, err = self._exit_code_and_err(self._args())
        self.assertEqual(code, 2)
        self.assertIn("scrub_env", err)
        self.assertNotIn("close failed", err)
        self.connector.close.assert_called_once()

    # -- the context keywords ----------------------------------------------------------

    def test_factory_gets_only_the_keywords_its_signature_names(self) -> None:
        code, _out, _err = self._run(self._args("--timeout", "9", factory="repo_timeout_factory"))
        self.assertEqual(code, 0)
        self.assertEqual(_STATE["kwargs"], {"repo": str(self.repo), "timeout": 9})

    def test_kwargs_factory_gets_all_four_keywords(self) -> None:
        self._run(self._args("--runs-root", str(self.tmp / "r")))
        kwargs = _STATE["kwargs"]
        self.assertEqual(set(kwargs), {"repo", "mode", "timeout", "locate"})
        self.assertEqual(kwargs["repo"], str(self.repo))
        self.assertEqual(kwargs["mode"], "agg")
        self.assertEqual(kwargs["timeout"], 3600)
        found = kwargs["locate"]({"task_id": "T-900", "k": 1, "seed": None, "selections": {}})
        self.assertEqual(found["summary_json"].name, "summary.json")
        self.assertIn(str(self.tmp / "r"), str(found["summary_json"]))
        self.assertIn("T-900", str(found["summary_json"]))

    def test_connector_opt_wins_over_the_context(self) -> None:
        self._run(self._args("--connector-opt", "timeout=5", "--connector-opt", "url=a=b"))
        self.assertEqual(_STATE["kwargs"]["timeout"], "5")
        self.assertEqual(_STATE["kwargs"]["url"], "a=b")

    # -- optional capabilities ---------------------------------------------------------

    def test_judge_goes_to_the_intake_adapter(self) -> None:
        def judge(run_dir, task_id, criteria):
            return None

        self.connector.judge = lambda: judge
        self._run(self._args())
        adapter = self.run_intake.call_args.args[0]
        self.assertIs(adapter.judge, judge)

    def test_base_harness_goes_to_the_intake_adapter(self) -> None:
        self.connector.base_harness = lambda: {"systemprompt.md": "Follow worker.md.\n"}
        self._run(self._args())
        adapter = self.run_intake.call_args.args[0]
        text = (adapter.base_harness() / "systemprompt.md").read_text()
        self.assertEqual(text, "Follow worker.md.\n")

    def test_no_judge_means_no_scoring(self) -> None:
        self._run(self._args())
        self.assertIsNone(self.run_intake.call_args.args[0].judge)

    def test_workspace_policy_and_scrub_env_go_to_the_launcher(self) -> None:
        self.connector.scrub_env = ("SECRET_TOKEN_X",)
        self.connector.workspace_policy = WorkspacePolicy(exclude=("private", "notes.md"))
        code, _out, _err = self._run(self._args())
        self.assertEqual(code, 0)
        self.launcher_cls.assert_called_once()
        call = self.launcher_cls.call_args
        self.assertEqual(call.args[0], self.repo)
        self.assertEqual(call.kwargs["exclude"], ("private", "notes.md"))
        self.assertEqual(call.kwargs["scrub_env"], ("SECRET_TOKEN_X",))
        self.assertNotIn("env", call.kwargs)

    def test_without_capabilities_the_launcher_gets_empty_exclude_and_scrub_env(self) -> None:
        self._run(self._args())
        call = self.launcher_cls.call_args
        self.assertEqual(call.kwargs["exclude"], ())
        self.assertEqual(call.kwargs["scrub_env"], ())

    def test_report_text_is_printed_when_the_connector_has_one(self) -> None:
        self.connector.report = lambda task_id: f"report of {task_id}"
        self._outcomes(("T-1", "DONE"), ("T-2", "DONE"))
        code, out, _err = self._run(self._args())
        self.assertEqual(code, 0)
        self.assertEqual(out, "report of T-1\n\nreport of T-2\n")

    def test_report_none_falls_back_to_the_status_line(self) -> None:
        self.connector.report = lambda task_id: None
        self._outcomes(("T-1", "DONE"))
        _code, out, _err = self._run(self._args())
        self.assertEqual(out, "T-1: DONE\n")

    def test_status_lines_without_report(self) -> None:
        self._outcomes(("T-1", "DONE"), ("T-2", "BLOCKED"))
        _code, out, _err = self._run(self._args())
        self.assertEqual(out, "T-1: DONE\n\nT-2: BLOCKED\n")
        self.assertEqual(self.connector.delivered, ["T-1"])
        self.assertEqual(self.connector.failed, ["T-2"])

    def test_nothing_taken_up_is_said_on_stderr(self) -> None:
        code, out, err = self._run(self._args())
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("no task was taken up", err)

    def test_close_and_cleanup_run_on_success(self) -> None:
        self.connector.close = mock.MagicMock()
        code, _out, _err = self._run(self._args())
        self.assertEqual(code, 0)
        self.connector.close.assert_called_once()
        self.launcher.cleanup.assert_called_once()

    def test_close_and_cleanup_run_when_run_intake_raises(self) -> None:
        self.connector.close = mock.MagicMock()
        self.run_intake.side_effect = RuntimeError("boom")
        code, _out, err = self._run(self._args())
        self.assertEqual(code, 1)
        self.assertIn("error: boom", err)
        self.connector.close.assert_called_once()
        self.launcher.cleanup.assert_called_once()

    def test_close_runs_when_cleanup_raises(self) -> None:
        self.connector.close = mock.MagicMock()
        self.launcher.cleanup.side_effect = RuntimeError("cleanup failed")
        code, _out, err = self._run(self._args())
        self.assertEqual(code, 1)
        self.assertIn("cleanup failed", err)
        self.connector.close.assert_called_once()

    def test_a_failing_cleanup_does_not_hide_the_run_error(self) -> None:
        self.run_intake.side_effect = RuntimeError("boom")
        self.launcher.cleanup.side_effect = RuntimeError("cleanup failed")
        code, _out, err = self._run(self._args())
        self.assertEqual(code, 1)
        self.assertIn("error: boom", err)

    # -- reports and close -------------------------------------------------------------

    def _use(self, connector: FakeConnector) -> None:
        self.connector = connector
        _STATE["connector"] = connector

    def test_reports_are_taken_before_close(self) -> None:
        self._use(ClosingConnector())
        self._outcomes(("T-1", "DONE"), ("T-2", "DONE"))
        code, out, err = self._run(self._args())
        self.assertEqual(code, 0)
        self.assertEqual(self.connector.calls, ["report T-1", "report T-2", "close"])
        self.assertEqual(out, "report of T-1\n\nreport of T-2\n")
        self.assertEqual(err, "")

    def test_one_failing_report_falls_back_to_the_status_line(self) -> None:
        self._use(ClosingConnector(fail_report="T-1"))
        self._outcomes(("T-1", "DONE"), ("T-2", "DONE"))
        code, out, err = self._run(self._args())
        self.assertEqual(code, 1)
        self.assertEqual(out, "T-1: DONE\n\nreport of T-2\n")
        self.assertIn("error: report T-1: server went away", err)
        self.assertEqual(self.connector.calls[-1], "close")

    def test_run_error_is_printed_when_a_report_also_fails(self) -> None:
        self._use(ClosingConnector(fail_report="T-1"))
        self._outcomes(("T-1", "DONE"), then=RuntimeError("lost the server"))
        code, out, err = self._run(self._args())
        self.assertEqual(code, 1)
        self.assertEqual(out, "T-1: DONE\n")
        self.assertIn("error: report T-1: server went away", err)
        self.assertIn("error: lost the server", err)
        self.assertNotIn("Traceback", err)

    def test_a_failing_close_hides_neither_the_reports_nor_the_run_error(self) -> None:
        self.connector.report = lambda task_id: f"report of {task_id}"
        self.connector.close = mock.MagicMock(side_effect=ConnectionError("close failed"))
        self._outcomes(("T-1", "DONE"), then=RuntimeError("lost the server"))
        code, out, err = self._run(self._args())
        self.assertEqual(code, 1)
        self.assertEqual(out, "report of T-1\n")
        self.assertIn("error: lost the server", err)
        self.assertIn("close failed", err)

    def test_a_failing_close_alone_is_exit_1_after_the_reports(self) -> None:
        self.connector.close = mock.MagicMock(side_effect=ConnectionError("close failed"))
        self._outcomes(("T-1", "DONE"))
        code, out, err = self._run(self._args())
        self.assertEqual(code, 1)
        self.assertEqual(out, "T-1: DONE\n")
        self.assertIn("close failed", err)

    # -- a failure part way, and the exit code -----------------------------------------

    def test_task_delivered_before_an_exception_is_still_printed(self) -> None:
        self._outcomes(("T-1", "DONE"), then=RuntimeError("lost the server"))
        code, out, err = self._run(self._args())
        self.assertEqual(code, 1)
        self.assertEqual(out, "T-1: DONE\n")
        self.assertIn("error: lost the server", err)

    def _deliver_then_fail_on_error(self, task_id: str) -> None:
        """A run_intake that, like the real one, calls fail when deliver raises."""

        def fake(adapter, prop, **kwargs):
            conn = adapter.connector
            try:
                conn.deliver(task_id, self.tmp / "d" / task_id, self._done_summary(task_id))
            except Exception as exc:
                conn.fail(task_id, str(exc))
            return []

        self.run_intake.side_effect = fake

    def test_deliver_raising_then_fail_is_blocked_not_done(self) -> None:
        def deliver(task_id, deliverable_dir, summary):
            raise ConnectionError("gone")

        self.connector.deliver = deliver
        self._deliver_then_fail_on_error("T-1")
        code, out, _err = self._run(self._args())
        self.assertEqual(code, 1)
        self.assertEqual(out, "T-1: BLOCKED\n")
        self.assertEqual(self.connector.failed, ["T-1"])

    def test_deliver_and_fail_both_raising_is_not_done(self) -> None:
        def raise_gone(*args):
            raise ConnectionError("gone")

        self.connector.deliver = raise_gone
        self.connector.fail = raise_gone
        self._deliver_then_fail_on_error("T-1")
        code, out, err = self._run(self._args())
        self.assertEqual(code, 1)
        self.assertNotIn("DONE", out)
        self.assertEqual(out, "T-1: BLOCKED\n")
        self.assertIn("error: gone", err)

    def test_all_done_is_exit_0(self) -> None:
        self._outcomes(("T-1", "DONE"), ("T-2", "DONE"))
        code, _out, _err = self._run(self._args())
        self.assertEqual(code, 0)

    def test_one_blocked_is_exit_1(self) -> None:
        self._outcomes(("T-1", "DONE"), ("T-2", "BLOCKED"))
        code, _out, _err = self._run(self._args())
        self.assertEqual(code, 1)

    # -- the model connector -----------------------------------------------------------

    def test_model_with_a_non_default_model_connector_is_exit_2(self) -> None:
        code, err = self._exit_code_and_err(
            self._args("--model-connector", _ref("model_factory"), "--model", "prov/x")
        )
        self.assertEqual(code, 2)
        self.assertIn("--model", err)
        self.assertNotIn("model_kwargs", _STATE)
        self.run_intake.assert_not_called()

    def test_model_connector_opt_without_equals_is_exit_2(self) -> None:
        code, err = self._exit_code_and_err(
            self._args("--model-connector", _ref("model_factory"), "--model-connector-opt", "url")
        )
        self.assertEqual(code, 2)
        self.assertIn("--model-connector-opt", err)

    def test_unknown_model_connector_option_is_exit_2(self) -> None:
        code, err = self._exit_code_and_err(
            self._args(
                "--model-connector",
                _ref("model_url_factory"),
                "--model-connector-opt",
                "other=1",
            )
        )
        self.assertEqual(code, 2)
        self.assertIn("other", err)
        self.run_intake.assert_not_called()

    def test_unknown_model_connector_is_exit_2(self) -> None:
        code, err = self._exit_code_and_err(self._args("--model-connector", "no-such-connector"))
        self.assertEqual(code, 2)
        self.assertIn("unknown model connector", err)

    def test_model_not_a_connector_is_exit_2(self) -> None:
        code, err = self._exit_code_and_err(
            self._args("--model-connector", _ref("model_not_a_connector"))
        )
        self.assertEqual(code, 2)
        self.assertIn("not a ModelConnector", err)

    def test_model_connector_options_reach_the_factory(self) -> None:
        code, _out, _err = self._run(
            self._args(
                "--model-connector",
                _ref("model_url_factory"),
                "--model-connector-opt",
                "url=a=b",
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(_STATE["model_kwargs"], {"url": "a=b"})

    def test_default_connector_with_model_forces_that_one_choice(self) -> None:
        code, _out, _err = self._run(self._args("--model", "prov/model-z"))
        self.assertEqual(code, 0)
        executor = cli.OpencodeAgent.call_args.args[0]
        self.assertIsInstance(executor, cli.ExecutorModels)
        self.assertEqual(executor.current("t1"), models.ModelChoice("opencode", "prov/model-z"))
        adapter = self.run_intake.call_args.args[0]
        self.assertEqual(adapter.model_records("t1"), ({}, ""))

    def test_default_connector_uses_the_configured_executor_model(self) -> None:
        with mock.patch.object(
            models.config, "route", return_value=mock.Mock(model="prov/env-model")
        ):
            code, _out, _err = self._run(self._args())
            self.assertEqual(code, 0)
            executor = cli.OpencodeAgent.call_args.args[0]
            # The candidates are asked for lazily, on the first rollout of the task.
            self.assertEqual(
                executor.current("t1"), models.ModelChoice("opencode", "prov/env-model")
            )
        adapter = self.run_intake.call_args.args[0]
        self.assertEqual(adapter.model_records("t1"), ({}, ""))

    def test_custom_model_connector_is_closed_and_named_in_the_summary(self) -> None:
        name = _ref("model_factory")
        code, _out, _err = self._run(self._args("--model-connector", name))
        self.assertEqual(code, 0)
        self.assertTrue(self.model_connector.closed)
        adapter = self.run_intake.call_args.args[0]
        self.assertEqual(adapter.model_connector, name)
        self.assertEqual(adapter.model_records("t1"), ({}, name))
        self.assertEqual(
            cli.OpencodeAgent.call_args.args[0].current("t1"),
            models.ModelChoice("opencode", "fake/model"),
        )

    def test_improver_candidate_claude_cli_is_the_claude_cli_channel(self) -> None:
        code, _out, _err = self._run(
            self._args("--model-connector", _ref("claude_cli_improver_factory"))
        )
        self.assertEqual(code, 0)
        args = cli.resolve_channel_model.call_args
        self.assertEqual(args.args, ("claude_cli", "opus"))
        self.assertEqual(args.kwargs["effort"], "low")

    def test_connector_with_no_runner_meeting_the_fence_is_exit_2(self) -> None:
        code, err = self._exit_code_and_err(
            self._args("--model-connector", _ref("loose_session_factory"))
        )
        self.assertEqual(code, 2)
        self.assertIn("commands", err)
        self.run_intake.assert_not_called()

    def test_model_connector_close_error_keeps_the_run_error(self) -> None:
        self.connector.close = mock.MagicMock()
        self.run_intake.side_effect = RuntimeError("boom")
        code, _out, err = self._run(self._args("--model-connector", _ref("model_close_raises")))
        self.assertEqual(code, 1)
        self.assertIn("error: boom", err)
        self.assertIn("error: model connector close: model close failed", err)
        self.connector.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
