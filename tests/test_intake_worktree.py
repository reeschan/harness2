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

import json
import os
import shutil
import subprocess
import tempfile
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

    def run(self, *, worktree, prompt, install, run_dir, timeout, env):
        self.calls.append(
            {"worktree": worktree, "prompt": prompt, "install": install, "env": dict(env)}
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


class FakeConnector:
    def __init__(self, raws):
        self.raws = list(raws)

    def poll(self):
        return list(self.raws)

    def fetch_files(self, raw, dest):
        pass

    def deliver(self, task_id, deliverable_dir, summary):
        pass

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


if __name__ == "__main__":
    unittest.main()
