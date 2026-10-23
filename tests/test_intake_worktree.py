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

"""benchmarks.intake_worktree on real git repositories, with a fake agent (no OpenCode)."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from harness2.benchmarks import intake_worktree as iw
from harness2.benchmarks.intake import IntakeAdapter, Launcher
from harness2.core import spec
from harness2.core import worktree as wt
from harness2.core.intake import RawTask
from harness2.core.normalize import PassthroughNormalizer
from harness2.substrate.base import Install


def git(cwd, *args):
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


class FakeAgent:
    """Applies `edits` (rel -> text) in the worktree; records each call."""

    def __init__(self, edits=None, error=None):
        self.edits = edits or {}
        self.error = error
        self.calls: list[dict] = []

    def run(self, *, worktree, prompt, install, run_dir, timeout, env, task_id="", reset=None):
        self.calls.append(
            {
                "worktree": worktree,
                "prompt": prompt,
                "install": install,
                "env": dict(env),
                "task_id": task_id,
                "reset": reset,
            }
        )
        if self.error is not None:
            raise self.error
        for rel, text in self.edits.items():
            path = Path(worktree) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (Path(run_dir) / "trajectory.md").write_text("[ACTION] edit\n", encoding="utf-8")


_GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
)


class Base(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
        )
        env.start()
        self.addCleanup(env.stop)
        for var in _GIT_LOCATION_VARS:
            os.environ.pop(var, None)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "src"
        self.src.mkdir()
        git(self.src, "init", "-q", "-b", "main")
        (self.src / "a.txt").write_text("alpha\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "init")
        self.run_dir = self.tmp / "executions" / "r1"
        self.workspace = self.run_dir / "workspace"
        (self.workspace / "output").mkdir(parents=True)
        self.diff = self.workspace / "output" / wt.DIFF_NAME

    def launch(self, launcher, prompt="do it"):
        launcher.launch(
            workspace=self.workspace,
            prompt=prompt,
            install=None,
            run_dir=self.run_dir,
            timeout=60,
        )


class WorktreeLauncherTest(Base):
    def test_is_a_launcher(self):
        self.assertIsInstance(iw.WorktreeLauncher(self.src, FakeAgent()), Launcher)
        self.assertIsInstance(FakeAgent(), iw.AgentRunner)
        self.assertIsInstance(iw.OpencodeAgent("m"), iw.AgentRunner)

    def test_change_becomes_diff_and_source_is_untouched(self):
        launcher = iw.WorktreeLauncher(self.src, FakeAgent({"a.txt": "alpha changed\n"}))
        self.addCleanup(launcher.cleanup)
        before = git(self.src, "status", "--porcelain")
        self.launch(launcher)
        self.assertTrue(self.diff.is_file())
        self.assertIn("+alpha changed", self.diff.read_text())
        self.assertEqual((self.src / "a.txt").read_text(), "alpha\n")
        self.assertEqual(git(self.src, "status", "--porcelain"), before)

    def test_no_change_writes_no_diff(self):
        launcher = iw.WorktreeLauncher(self.src, FakeAgent())
        self.addCleanup(launcher.cleanup)
        self.launch(launcher)
        self.assertFalse(self.diff.exists())

    def test_scrub_env_names_leave_and_the_rest_stays(self):
        agent = FakeAgent()
        launcher = iw.WorktreeLauncher(
            self.src,
            agent,
            env={"SECRET_A": "s", "SECRET_B": "t", "KEEP_ME": "1"},
            scrub_env=("SECRET_A", "SECRET_B", "NOT_SET"),
        )
        self.addCleanup(launcher.cleanup)
        self.launch(launcher)
        self.assertEqual(agent.calls[0]["env"], {"KEEP_ME": "1"})

    def test_scrub_env_applies_to_os_environ_when_env_is_none(self):
        agent = FakeAgent()
        launcher = iw.WorktreeLauncher(self.src, agent, scrub_env=["SECRET_A"])
        self.addCleanup(launcher.cleanup)
        with mock.patch.dict(os.environ, {"SECRET_A": "s", "KEEP_ME": "1"}):
            self.launch(launcher)
        env = agent.calls[0]["env"]
        self.assertNotIn("SECRET_A", env)
        self.assertEqual(env["KEEP_ME"], "1")

    def test_by_default_nothing_is_scrubbed(self):
        agent = FakeAgent()
        given = {"SECRET_A": "s", "SOME_API_KEY": "k", "KEEP_ME": "1"}
        launcher = iw.WorktreeLauncher(self.src, agent, env=given)
        self.addCleanup(launcher.cleanup)
        self.launch(launcher)
        self.assertEqual(agent.calls[0]["env"], given)

    def test_exclude_leaves_paths_out_of_the_diff(self):
        edits = {"a.txt": "alpha changed\n", "secret/x.txt": "hidden\n"}
        launcher = iw.WorktreeLauncher(self.src, FakeAgent(edits), exclude=("secret",))
        self.addCleanup(launcher.cleanup)
        self.launch(launcher)
        text = self.diff.read_text()
        self.assertIn("+alpha changed", text)
        self.assertNotIn("secret/x.txt", text)
        self.assertNotIn("hidden", text)

    def test_without_exclude_every_change_is_in_the_diff(self):
        edits = {"a.txt": "alpha changed\n", "secret/x.txt": "hidden\n"}
        launcher = iw.WorktreeLauncher(self.src, FakeAgent(edits))
        self.addCleanup(launcher.cleanup)
        self.launch(launcher)
        self.assertIn("secret/x.txt", self.diff.read_text())

    def test_exclude_only_leaving_changes_writes_no_diff(self):
        launcher = iw.WorktreeLauncher(
            self.src, FakeAgent({"secret/x.txt": "hidden\n"}), exclude=("secret",)
        )
        self.addCleanup(launcher.cleanup)
        self.launch(launcher)
        self.assertFalse(self.diff.exists())

    def test_prompt_starts_with_preamble(self):
        agent = FakeAgent()
        launcher = iw.WorktreeLauncher(self.src, agent)
        self.addCleanup(launcher.cleanup)
        self.launch(launcher, prompt="TASK BODY")
        prompt = agent.calls[0]["prompt"]
        self.assertTrue(prompt.startswith(iw.WORKTREE_PREAMBLE))
        self.assertTrue(prompt.endswith("TASK BODY"))
        self.assertEqual(agent.calls[0]["worktree"], self.run_dir / wt.WORKTREE_DIRNAME)

    def test_second_launch_in_same_run_dir(self):
        launcher = iw.WorktreeLauncher(self.src, FakeAgent({"a.txt": "one\n"}))
        self.addCleanup(launcher.cleanup)
        self.launch(launcher)
        self.diff.unlink()
        launcher.agent = FakeAgent({"a.txt": "two\n"})
        self.launch(launcher)
        self.assertIn("+two", self.diff.read_text())
        self.assertEqual(len(launcher.created), 1)

    def test_launch_after_run_dir_was_deleted(self):
        launcher = iw.WorktreeLauncher(self.src, FakeAgent({"a.txt": "one\n"}))
        self.addCleanup(launcher.cleanup)
        self.launch(launcher)
        shutil.rmtree(self.run_dir)  # as IntakeAdapter.execute does with a stale run_dir
        (self.workspace / "output").mkdir(parents=True)
        self.launch(launcher)
        self.assertIn("+one", self.diff.read_text())

    def test_agent_error_propagates(self):
        launcher = iw.WorktreeLauncher(self.src, FakeAgent(error=ValueError("boom")))
        self.addCleanup(launcher.cleanup)
        with self.assertRaisesRegex(ValueError, "boom"):
            self.launch(launcher)
        self.assertFalse(self.diff.exists())

    def test_cleanup_removes_every_worktree(self):
        launcher = iw.WorktreeLauncher(self.src, FakeAgent({"a.txt": "x\n"}))
        self.launch(launcher)
        other = self.tmp / "executions" / "r2"
        (other / "workspace" / "output").mkdir(parents=True)
        launcher.launch(
            workspace=other / "workspace", prompt="p", install=None, run_dir=other, timeout=60
        )
        made = list(launcher.created)
        self.assertEqual(len(made), 2)
        launcher.cleanup()
        for path in made:
            self.assertFalse(path.exists())
        self.assertNotIn("r1", git(self.src, "worktree", "list"))
        self.assertNotIn("r2", git(self.src, "worktree", "list"))
        launcher.cleanup()

    def test_launch_passes_task_id_to_the_agent(self):
        agent = FakeAgent()
        launcher = iw.WorktreeLauncher(self.src, agent)
        self.addCleanup(launcher.cleanup)
        launcher.launch(
            workspace=self.workspace,
            prompt="p",
            install=None,
            run_dir=self.run_dir,
            timeout=60,
            task_id="PBI-001",
        )
        self.assertEqual(agent.calls[0]["task_id"], "PBI-001")
        self.assertTrue(callable(agent.calls[0]["reset"]))


class FakeConnector:
    def __init__(self, raws):
        self.raws = list(raws)
        self.delivered: list[str] = []
        self.summaries: dict[str, dict] = {}

    def poll(self):
        return list(self.raws)

    def fetch_files(self, raw, dest):
        pass

    def deliver(self, task_id, deliverable_dir, summary):
        self.delivered.append(task_id)
        self.summaries[task_id] = summary

    def fail(self, task_id, reason):
        raise AssertionError(f"{task_id} failed: {reason}")


class AdapterTest(Base):
    def test_execute_through_intake_adapter(self):
        task = {
            "task_id": "PBI-001",
            "domain": "code",
            "prompt": "Change a.txt.",
            "files": [],
            "deliverable": {"what": "a diff"},
            "prompts": {"work": "change code"},
        }
        raw = RawTask(id="PBI-001", meta={"task_spec": task})
        launcher = iw.WorktreeLauncher(self.src, FakeAgent({"a.txt": "changed\n"}))
        self.addCleanup(launcher.cleanup)
        adapter = IntakeAdapter(
            self.tmp / "root",
            FakeConnector([raw]),
            PassthroughNormalizer(),
            launcher=launcher,
            channel=spec.ChannelSpec("opencode", "fake-model"),
        )
        self.assertEqual(adapter.ingest(), ["PBI-001"])
        run_dir = adapter.execute("PBI-001", None, "run-1")
        self.assertTrue(adapter.delivered(run_dir))
        self.assertIn(wt.DIFF_NAME, adapter.deliverable_files(run_dir))
        self.assertTrue((run_dir / wt.WORKTREE_DIRNAME).is_dir())


class FakeProc:
    def __init__(self, stdout, rc=0, hang=False, interrupt=False):
        self.pid = 4242
        self.rc = rc
        self.hang = hang
        self.interrupt = interrupt
        self.killed = False
        stdout.write((json.dumps({"part": {"type": "text", "text": "EVENT-TEXT"}}) + "\n").encode())

    def wait(self, timeout=None):
        if self.interrupt and timeout is not None:
            raise KeyboardInterrupt
        if self.hang and timeout is not None:
            raise subprocess.TimeoutExpired("opencode", timeout)
        return self.rc

    def kill(self):
        self.killed = True


class OpencodeAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wt = self.tmp / "run" / "worktree"
        self.wt.mkdir(parents=True)
        self.run_dir = self.tmp / "run"
        self.calls: list[tuple[list, dict]] = []
        self.rc = 0
        self.hang = False
        self.interrupt = False

    def _popen(self, argv, **kw):
        self.calls.append((argv, kw))
        return FakeProc(kw["stdout"], rc=self.rc, hang=self.hang, interrupt=self.interrupt)

    def _run(self, install=None, prompt="PROMPT-BODY"):
        with mock.patch.object(iw.subprocess, "Popen", side_effect=self._popen):
            iw.OpencodeAgent("prov/model-x").run(
                worktree=self.wt,
                prompt=prompt,
                install=install,
                run_dir=self.run_dir,
                timeout=30,
                env={"KEEP": "1"},
            )

    def test_argv_env_and_trajectory(self):
        self._run()
        argv, kw = self.calls[0]
        i = argv.index("--dir")
        self.assertEqual(argv[i + 1], str(self.wt))
        self.assertIn("prov/model-x", argv)
        self.assertIn("PROMPT-BODY", argv)
        self.assertIn("OPENCODE_PERMISSION", kw["env"])
        self.assertEqual(kw["env"]["KEEP"], "1")
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertTrue(kw["start_new_session"])
        self.assertIn("EVENT-TEXT", (self.run_dir / "trajectory.md").read_text())
        self.assertFalse((self.wt / "AGENTS.md").exists())

    def test_install_puts_harness_in_prompt_not_in_worktree(self):
        staging = self.tmp / "staging"
        staging.mkdir()
        (staging / "systemprompt.md").write_text("HARNESS-SYSTEM-LINE\n")
        self._run(install=Install(staging_dir=staging, fingerprint="fp"))
        argv, _ = self.calls[0]
        prompt = next(a for a in argv if "HARNESS-SYSTEM-LINE" in a)
        self.assertTrue(prompt.endswith("PROMPT-BODY"))
        self.assertNotIn("{{ instruction }}", prompt)
        self.assertFalse((self.wt / "AGENTS.md").exists())
        self.assertEqual(sorted(p.name for p in self.wt.iterdir()), [])

    def test_nonzero_exit_raises(self):
        self.rc = 3
        with self.assertRaisesRegex(RuntimeError, "status 3"):
            self._run()
        self.assertTrue((self.run_dir / "trajectory.md").is_file())

    def test_timeout_kills_group_and_raises(self):
        self.hang = True
        with mock.patch.object(iw.os, "killpg") as killpg:
            with self.assertRaises(TimeoutError):
                self._run()
        killpg.assert_called_once()
        self.assertEqual(killpg.call_args.args[0], 4242)

    def test_keyboard_interrupt_kills_group_and_reraises(self):
        self.interrupt = True
        with (
            mock.patch.object(iw.os, "killpg") as killpg,
            mock.patch.object(iw, "_kill_group", wraps=iw._kill_group) as kill_group,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self._run()
        kill_group.assert_called_once()
        killpg.assert_called_once()
        self.assertEqual(killpg.call_args.args[0], 4242)
        self.assertTrue((self.run_dir / "trajectory.md").is_file())

    def test_timeout_is_still_a_timeout_error(self):
        self.hang = True
        with (
            mock.patch.object(iw.os, "killpg"),
            mock.patch.object(iw, "_kill_group", wraps=iw._kill_group) as kill_group,
        ):
            with self.assertRaisesRegex(TimeoutError, "within 30s"):
                self._run()
        kill_group.assert_called_once()

    def test_missing_events_writes_error(self):
        iw.OpencodeAgent("m")._write_trajectory(self.run_dir)
        self.assertEqual((self.run_dir / "trajectory.md").read_text(), "[ERROR] no events recorded")


def _error(message):
    return {"type": "error", "error": {"message": message}}


def _api_error(*, name="APIError", message="", status=None, data=None, headers=None):
    """A structured provider error event: `error.name` and `error.data.*`."""
    body = dict(data or {})
    if message:
        body["message"] = message
    if status is not None:
        body["statusCode"] = status
    error = {"name": name, "data": body}
    if headers is not None:
        error["responseHeaders"] = headers
    return {"type": "error", "error": error}


def _step(cost=0.0, input_tokens=None, output_tokens=None):
    tokens = {}
    if input_tokens is not None:
        tokens["input"] = input_tokens
    if output_tokens is not None:
        tokens["output"] = output_tokens
    return {
        "type": "step_finish",
        "part": {"type": "step_finish", "cost": cost, "tokens": tokens},
    }


def _text(text="done"):
    return {"part": {"type": "text", "text": text}}


class FakeModelConnector:
    """routes per task; records every `record` call, optionally raising."""

    def __init__(self, by_task, record_error=None):
        self.by_task = by_task
        self.record_error = record_error
        self.calls: list[tuple[str, str, str]] = []
        self.recorded: list[tuple] = []

    def routes(self, role, *, task_id, substrate):
        self.calls.append((role, task_id, substrate))
        return list(self.by_task.get(task_id, []))

    def record(self, role, task_id, choice, outcome):
        self.recorded.append((role, task_id, choice, outcome))
        if self.record_error is not None:
            raise self.record_error


class ScriptedOpencode:
    """A Popen stand-in: writes a scripted JSON event stream and returns a status."""

    def __init__(self, stdout, status, events, hang=False):
        self.pid = 4242
        self.status = status
        self.hang = hang
        for event in events:
            stdout.write((json.dumps(event) + "\n").encode())

    def wait(self, timeout=None):
        if self.hang and timeout is not None:
            raise subprocess.TimeoutExpired("opencode", timeout)
        return self.status

    def kill(self):
        pass


class UnavailableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.events = self.tmp / "events.jsonl"

    def _write(self, *events):
        self.events.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    def test_usage_limit_is_unavailable(self):
        self._write(_error("429 Too Many Requests: rate limit exceeded"))
        self.assertTrue(iw.is_unavailable(1, self.events))

    def test_authentication_is_unavailable(self):
        self._write(_error("authentication failed: invalid api key"))
        self.assertTrue(iw.is_unavailable(1, self.events))

    def test_variant_unavailable_is_unavailable(self):
        self._write(_error("provider.no-route: Variant unavailable"))
        self.assertTrue(iw.is_unavailable(1, self.events))

    def test_status_code_is_read_as_a_number(self):
        for code in (401, 403, 429, 529):
            with self.subTest(code=code):
                self._write(_api_error(status=code, message="the provider said no"))
                self.assertTrue(iw.is_unavailable(1, self.events))

    def test_a_status_code_written_as_text_is_not_a_code(self):
        self._write(_api_error(status="429", message="the provider said no"))
        self.assertFalse(iw.is_unavailable(1, self.events))

    def test_provider_auth_error_name_is_unavailable(self):
        self._write(_api_error(name="ProviderAuthError", message="try again later"))
        self.assertTrue(iw.is_unavailable(1, self.events))

    def test_provider_model_not_found_name_is_unavailable(self):
        self._write(_api_error(name="ProviderModelNotFoundError", message="unknown"))
        self.assertTrue(iw.is_unavailable(1, self.events))

    def test_the_error_inside_a_part_is_read(self):
        self._write({"part": {"type": "error", "error": {"name": "ProviderAuthError", "data": {}}}})
        self.assertTrue(iw.is_unavailable(1, self.events))

    def test_a_connection_header_on_a_400_is_not_unavailable(self):
        self._write(
            _api_error(
                status=400,
                message="the request was rejected",
                headers={"connection": "keep-alive"},
            )
        )
        self.assertFalse(iw.is_unavailable(1, self.events))

    def test_a_token_limit_of_500_is_not_unavailable(self):
        self._write(
            _api_error(
                message="max_tokens: 500 exceeds the limit",
                data={"max_tokens": 500},
            )
        )
        self.assertFalse(iw.is_unavailable(1, self.events))

    def test_a_provider_id_key_alone_is_not_unavailable(self):
        self._write({"type": "error", "error": {"data": {"providerID": "openai"}}})
        self.assertFalse(iw.is_unavailable(1, self.events))

    def test_plain_error_is_not_unavailable(self):
        self._write(_error("the tool call failed: boom"))
        self.assertFalse(iw.is_unavailable(1, self.events))

    def test_zero_exit_is_never_unavailable(self):
        self._write(_error("rate limit exceeded"))
        self.assertFalse(iw.is_unavailable(0, self.events))

    def test_no_error_event_is_not_unavailable(self):
        self._write(_text())
        self.assertFalse(iw.is_unavailable(1, self.events))


class ExecutorModelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wt = self.tmp / "run" / "worktree"
        self.wt.mkdir(parents=True)
        self.run_dir = self.tmp / "run"
        self.argv: list[list] = []
        self.scripts: list[tuple] = []

    def _popen(self, argv, **kw):
        self.argv.append(argv)
        status, events, hang = self.scripts.pop(0)
        return ScriptedOpencode(kw["stdout"], status, events, hang)

    def _agent(self, connector):
        return iw.OpencodeAgent(iw.ExecutorModels(connector, name="fake"))

    def _run(self, agent, task_id="t1"):
        with mock.patch.object(iw.subprocess, "Popen", side_effect=self._popen):
            agent.run(
                worktree=self.wt,
                prompt="P",
                install=None,
                run_dir=self.run_dir,
                timeout=30,
                env={},
                task_id=task_id,
            )

    def test_routes_is_called_once_and_the_second_candidate_runs(self):
        connector = FakeModelConnector(
            {
                "t1": [
                    iw.models.ModelChoice("opencode", "bad", "low"),
                    iw.models.ModelChoice("opencode", "good", "high"),
                ]
            }
        )
        self.scripts = [
            (3, [_error("rate limit exceeded"), _step(0.01, 5, 2)], False),
            (0, [_text(), _step(0.02, 7, 3)], False),
        ]
        self._run(self._agent(connector))
        self.assertEqual(connector.calls, [("executor", "t1", "opencode")])
        self.assertEqual(len(self.argv), 2)
        self.assertIn("bad", self.argv[0])
        self.assertEqual(self.argv[0][self.argv[0].index("--variant") + 1], "low")
        self.assertIn("good", self.argv[1])
        self.assertEqual(self.argv[1][self.argv[1].index("--variant") + 1], "high")
        self.assertEqual([c[2].model for c in connector.recorded], ["bad", "good"])
        self.assertTrue(connector.recorded[0][3].unavailable)
        self.assertFalse(connector.recorded[0][3].ok)
        self.assertTrue(connector.recorded[1][3].ok)
        self.assertEqual(connector.recorded[0][3].run_id, self.run_dir.name)

    def test_summary_rows_keep_the_tried_order_and_the_cost(self):
        connector = FakeModelConnector(
            {
                "t1": [
                    iw.models.ModelChoice("opencode", "bad", "low"),
                    iw.models.ModelChoice("opencode", "good", "high"),
                ]
            }
        )
        selector = iw.ExecutorModels(connector, name="fake")
        self.scripts = [
            (3, [_error("429 Too Many Requests"), _step(0.01, 5, 2)], False),
            (0, [_text(), _step(0.02, 7, 3)], False),
        ]
        self._run(iw.OpencodeAgent(selector))
        rows = selector.records_for("t1")
        self.assertEqual([row["model"] for row in rows], ["bad", "good"])
        self.assertEqual([row["n"] for row in rows], [1, 1])
        self.assertTrue(rows[0]["unavailable"])
        self.assertFalse(rows[0]["ok"])
        self.assertTrue(rows[1]["ok"])
        self.assertEqual(rows[0]["cost_usd"], 0.01)
        self.assertEqual(rows[1]["tokens"], {"input": 7, "output": 3})
        self.assertEqual(
            [row["runner"] for row in rows],
            ["opencode", "opencode"],
        )

    def test_other_failures_do_not_advance(self):
        connector = FakeModelConnector(
            {
                "t1": [
                    iw.models.ModelChoice("opencode", "bad"),
                    iw.models.ModelChoice("opencode", "good"),
                ]
            }
        )
        selector = iw.ExecutorModels(connector, name="fake")
        self.scripts = [(3, [_error("the tool call failed: boom")], False)]
        with self.assertRaisesRegex(RuntimeError, "status 3"):
            self._run(iw.OpencodeAgent(selector))
        self.assertEqual(len(self.argv), 1)
        self.assertEqual(len(connector.recorded), 1)
        self.assertFalse(selector.records_for("t1")[0]["unavailable"])
        self.assertEqual(connector.calls, [("executor", "t1", "opencode")])

    def test_exhausted_candidates_raise_the_last_failure(self):
        connector = FakeModelConnector(
            {
                "t1": [
                    iw.models.ModelChoice("opencode", "one"),
                    iw.models.ModelChoice("opencode", "two"),
                ]
            }
        )
        selector = iw.ExecutorModels(connector, name="fake")
        self.scripts = [
            (3, [_error("rate limit exceeded")], False),
            (4, [_error("authentication failed")], False),
        ]
        with self.assertRaisesRegex(RuntimeError, "status 4"):
            self._run(iw.OpencodeAgent(selector))
        self.assertEqual(len(self.argv), 2)
        self.assertEqual([row["model"] for row in selector.records_for("t1")], ["one", "two"])
        self.assertEqual(len(connector.recorded), 2)

    def test_a_second_unavailable_failure_ends_the_rollout(self):
        connector = FakeModelConnector(
            {
                "t1": [
                    iw.models.ModelChoice("opencode", "one"),
                    iw.models.ModelChoice("opencode", "two"),
                    iw.models.ModelChoice("opencode", "three"),
                ]
            }
        )
        selector = iw.ExecutorModels(connector, name="fake")
        self.scripts = [
            (3, [_error("rate limit exceeded")], False),
            (4, [_error("usage limit reached")], False),
        ]
        with self.assertRaisesRegex(RuntimeError, "status 4"):
            self._run(iw.OpencodeAgent(selector))
        # A rollout is retried once; the third candidate is not tried by this rollout.
        self.assertEqual(len(self.argv), 2)
        self.assertNotIn("three", self.argv[1])
        # The position did move for the rollouts still to come.
        self.assertEqual(selector.current("t1").model, "three")
        self.assertEqual([row["model"] for row in selector.records_for("t1")], ["one", "two"])

    def test_candidates_for_other_runners_are_skipped_with_a_reason(self):
        connector = FakeModelConnector(
            {
                "t1": [
                    iw.models.ModelChoice("claude", "claude-model"),
                    iw.models.ModelChoice("opencode", "good"),
                ]
            }
        )
        selector = iw.ExecutorModels(connector, name="fake")
        self.scripts = [(0, [_text()], False)]
        self._run(iw.OpencodeAgent(selector))
        self.assertEqual(len(self.argv), 1)
        self.assertIn("good", self.argv[0])
        self.assertNotIn("claude-model", self.argv[0])
        rows = selector.records_for("t1")
        self.assertEqual([row["model"] for row in rows], ["good", "claude-model"])
        self.assertEqual(rows[0]["n"], 1)
        self.assertEqual(rows[1]["n"], 0)
        self.assertEqual(rows[1]["skipped_reason"], "runner claude is not opencode")
        self.assertEqual([c[2].model for c in connector.recorded], ["good"])

    def test_only_candidates_for_other_runners_raise_with_the_count(self):
        connector = FakeModelConnector(
            {
                "t1": [
                    iw.models.ModelChoice("claude", "claude-model"),
                    iw.models.ModelChoice("codex", "codex-model"),
                ]
            }
        )
        selector = iw.ExecutorModels(connector, name="fake")
        with self.assertRaisesRegex(
            RuntimeError, r"no executor model.*\(2 candidates for other runners\)"
        ):
            self._run(iw.OpencodeAgent(selector))
        self.assertEqual(self.argv, [])
        rows = selector.records_for("t1")
        self.assertEqual([row["n"] for row in rows], [0, 0])
        self.assertTrue(all("skipped_reason" in row for row in rows))

    def test_parallel_rollouts_on_the_same_candidate_advance_only_once(self):
        connector = FakeModelConnector(
            {
                "t1": [
                    iw.models.ModelChoice("opencode", "a-model"),
                    iw.models.ModelChoice("opencode", "b-model"),
                    iw.models.ModelChoice("opencode", "c-model"),
                ]
            }
        )
        selector = iw.ExecutorModels(connector, name="fake")
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        attempts: dict[str, int] = {}
        calls: list[list] = []

        def popen(argv, **kw):
            runs = str(kw["stdout"].name)
            with lock:
                calls.append(list(argv))
                attempts[runs] = attempts.get(runs, 0) + 1
                attempt = attempts[runs]
            if attempt == 1:
                # Both rollouts must fail on the same candidate before either moves on.
                barrier.wait(timeout=10)
                return ScriptedOpencode(kw["stdout"], 3, [_error("rate limit exceeded")])
            return ScriptedOpencode(kw["stdout"], 0, [_text()])

        errors: list[BaseException] = []

        def rollout(index: int) -> None:
            run_dir = self.tmp / "parallel" / f"r{index}"
            worktree = run_dir / "worktree"
            worktree.mkdir(parents=True)
            try:
                iw.OpencodeAgent(selector).run(
                    worktree=worktree,
                    prompt="P",
                    install=None,
                    run_dir=run_dir,
                    timeout=30,
                    env={},
                    task_id="t1",
                )
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(iw.subprocess, "Popen", side_effect=popen):
            threads = [threading.Thread(target=rollout, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        models = [argv[argv.index("--model") + 1] for argv in calls]
        self.assertEqual(models.count("a-model"), 2)
        self.assertEqual(models.count("b-model"), 2)
        self.assertEqual(models.count("c-model"), 0)
        self.assertEqual(selector.current("t1").model, "b-model")
        rows = {row["model"]: row for row in selector.records_for("t1")}
        self.assertEqual(rows["a-model"]["n"], 2)
        self.assertTrue(rows["a-model"]["unavailable"])
        self.assertEqual(rows["b-model"]["n"], 2)
        self.assertTrue(rows["b-model"]["ok"])

    def test_empty_candidates_fail_before_the_rollout(self):
        connector = FakeModelConnector({})
        with self.assertRaisesRegex(RuntimeError, "no executor model"):
            self._run(self._agent(connector))
        self.assertEqual(self.argv, [])
        self.assertEqual(connector.recorded, [])
        self.assertEqual(connector.calls, [("executor", "t1", "opencode")])

    def test_timeout_does_not_advance(self):
        connector = FakeModelConnector(
            {
                "t1": [
                    iw.models.ModelChoice("opencode", "bad"),
                    iw.models.ModelChoice("opencode", "good"),
                ]
            }
        )
        selector = iw.ExecutorModels(connector, name="fake")
        self.scripts = [(0, [_text()], True)]
        with mock.patch.object(iw.os, "killpg"):
            with self.assertRaises(TimeoutError):
                self._run(iw.OpencodeAgent(selector))
        self.assertEqual(len(self.argv), 1)
        self.assertEqual(len(connector.recorded), 1)
        rows = selector.records_for("t1")
        self.assertEqual([row["model"] for row in rows], ["bad"])
        self.assertFalse(rows[0]["unavailable"])

    def test_record_failure_is_only_a_warning(self):
        connector = FakeModelConnector(
            {"t1": [iw.models.ModelChoice("opencode", "only")]},
            record_error=RuntimeError("runs.jsonl is read-only"),
        )
        selector = iw.ExecutorModels(connector, name="fake")
        self.scripts = [(0, [_text()], False)]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self._run(iw.OpencodeAgent(selector))
        self.assertIn("warning: model connector record failed", err.getvalue())
        self.assertTrue(selector.records_for("t1")[0]["ok"])


class RetryTraceTest(Base):
    """A retried rollout starts from the base worktree and keeps the failed attempt's trace."""

    class ScriptedAgent(iw.OpencodeAgent):
        def __init__(self, model):
            super().__init__(model)
            self.calls: list[dict] = []

        def _run_once(self, argv, run_dir, events, timeout, env):
            worktree = Path(argv[argv.index("--dir") + 1])
            attempt = len(self.calls) + 1
            self.calls.append(
                {
                    "attempt": attempt,
                    "leftover": (worktree / "leftover.txt").exists(),
                    "a": (worktree / "a.txt").read_text(encoding="utf-8"),
                }
            )
            if attempt == 1:
                (worktree / "leftover.txt").write_text("first attempt\n", encoding="utf-8")
                (worktree / "a.txt").write_text("half done\n", encoding="utf-8")
                events.write_text(json.dumps(_error("rate limit exceeded")) + "\n")
                (run_dir / "opencode.stderr").write_text("first stderr\n", encoding="utf-8")
                status = 3
            else:
                (worktree / "a.txt").write_text("done\n", encoding="utf-8")
                events.write_text(json.dumps(_text()) + "\n")
                (run_dir / "opencode.stderr").write_text("second stderr\n", encoding="utf-8")
                status = 0
            self._write_trajectory(run_dir)
            return status

    def test_retry_resets_the_worktree_and_keeps_the_failed_attempt(self):
        connector = FakeModelConnector(
            {
                "task-1": [
                    iw.models.ModelChoice("opencode", "bad"),
                    iw.models.ModelChoice("opencode", "good"),
                ]
            }
        )
        selector = iw.ExecutorModels(connector, name="fake")
        agent = self.ScriptedAgent(selector)
        launcher = iw.WorktreeLauncher(self.src, agent)
        self.addCleanup(launcher.cleanup)
        launcher.launch(
            workspace=self.workspace,
            prompt="change a.txt",
            install=None,
            run_dir=self.run_dir,
            timeout=60,
            task_id="task-1",
        )
        self.assertEqual(len(agent.calls), 2)
        # The retry saw the base worktree, without the failed attempt's half-written files.
        self.assertFalse(agent.calls[1]["leftover"])
        self.assertEqual(agent.calls[1]["a"], "alpha\n")
        self.assertFalse((self.run_dir / wt.WORKTREE_DIRNAME / "leftover.txt").exists())
        # The failed attempt's trace stays beside the last attempt's files.
        failed_events = (self.run_dir / "events.1.jsonl").read_text(encoding="utf-8")
        self.assertIn("rate limit", failed_events)
        self.assertEqual(
            (self.run_dir / "opencode.1.stderr").read_text(encoding="utf-8"), "first stderr\n"
        )
        self.assertTrue((self.run_dir / "trajectory.1.md").is_file())
        last_events = (self.run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("done", last_events)
        self.assertNotIn("rate limit", last_events)
        self.assertEqual(
            (self.run_dir / "opencode.stderr").read_text(encoding="utf-8"), "second stderr\n"
        )
        diff = self.diff.read_text(encoding="utf-8")
        self.assertIn("+done", diff)
        self.assertNotIn("leftover", diff)
        self.assertNotIn("half done", diff)
        self.assertEqual([row["model"] for row in selector.records_for("task-1")], ["bad", "good"])


if __name__ == "__main__":
    unittest.main()
