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

"""Intake end to end: local connector -> passthrough -> run_task -> delivery.

A fake launcher and a fake improver stand in for the agent and the model, so the test needs
no network. The private criteria carry a password that must reach only the judge.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from harness2.benchmarks import intake as intake_mod
from harness2.benchmarks.intake import IntakeAdapter, run_intake
from harness2.core import schedule
from harness2.core.intake_local import LocalFileConnector
from harness2.core.normalize import PassthroughNormalizer
from harness2.core.proposer import Proposer, Reply
from harness2.core.spec import ChannelSpec

SECRET = "SECRET-CRITERION-7f3a"
CHANNEL = ChannelSpec("opencode", "fake-model")
IMPROVED_LINE = "Check the totals against the attachment before writing output/."


def _task_spec(task_id: str, domain: str, work: str, where: str) -> dict:
    return {
        "task_id": task_id,
        "domain": domain,
        "prompt": f"Answer task {task_id} from the attached files.",
        "files": [],
        "deliverable": {"what": "a short answer", "where": where},
        "prompts": {"work": work},
        "private_criteria": [f"The answer satisfies {SECRET}."],
    }


class FakeLauncher:
    """Reads the workspace attachments, writes output/answer.txt and a trajectory."""

    def __init__(self, link_output: Path | None = None):
        self.calls = 0
        self.link_output = link_output

    def launch(self, *, workspace, prompt, install, run_dir, timeout, task_id=""):
        self.calls += 1
        attachments = sorted(
            p
            for p in workspace.rglob("*")
            if p.is_file() and "output" not in p.relative_to(workspace).parts
        )
        seen = "".join(p.read_text(encoding="utf-8", errors="replace") for p in attachments)
        system = ""
        if install is not None and (install.staging_dir / "systemprompt.md").is_file():
            system = (install.staging_dir / "systemprompt.md").read_text(encoding="utf-8")
        output = workspace / "output"
        if self.link_output is not None:
            output.rmdir()
            os.symlink(self.link_output, output)
        else:
            (output / "answer.txt").write_text(
                f"attachments: {len(attachments)}\n{seen}", encoding="utf-8"
            )
        (run_dir / "trajectory.md").write_text(
            f"[ACTION] read the attachments\n[OBSERVATION]\n{seen}\n"
            f"[ACTION] follow the harness\n[OBSERVATION]\n{system or '(base harness)'}\n",
            encoding="utf-8",
        )


class FakeImprover(Proposer):
    """Like demo.DemoProposer: scripted replies, and a record of everything it was shown."""

    def __init__(self, spec):
        super().__init__(spec)
        self.seen: list[str] = []

    def _record(self, mission, context_dir) -> None:
        self.seen.append(mission)
        if context_dir is None:
            return
        for path in sorted(Path(context_dir).rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            if b"\0" in data:
                continue
            self.seen.append(data.decode("utf-8", errors="replace"))

    def call(self, mission, context_dir, label, **kwargs) -> Reply:
        self._record(mission, context_dir)
        context = Path(context_dir) if context_dir is not None else None
        workspace = None
        if label.startswith("select."):
            candidates = sorted((context / "candidates").glob("*.md"))
            text = f"<why>The first candidate.</why>\n```choice\n{candidates[0].stem}\n```"
        elif label.startswith("experience."):
            text = "NO_ENTRY"
        else:
            workspace = context / "harness"
            prompt = workspace / "systemprompt.md"
            prompt.write_text(
                prompt.read_text(encoding="utf-8") + IMPROVED_LINE + "\n", encoding="utf-8"
            )
            text = "<notes>Check totals before writing the deliverable.</notes>"
        reply = Reply(label=label, text=text, ok=True, attempts=1, workspace=workspace)
        self._calls.append(reply)
        return reply


def _texts_under(base: Path, *, skip: Path | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not base.exists():
        return out
    for path in base.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if skip is not None and (path == skip or skip in path.parents):
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        out[str(path)] = data.decode("utf-8", errors="replace")
    return out


class IntakeEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.inbox_root = self.tmp / "connector"
        self.root = self.tmp / "intake"
        self.runs_root = self.tmp / "runs"
        self._write_inbox()

    def _write_inbox(self) -> None:
        good = self.inbox_root / "inbox" / "ledger-1"
        (good / "files").mkdir(parents=True)
        (good / "files" / "ledger.csv").write_text("amount\n12.50\n-2.25\n", encoding="utf-8")
        (good / "task.json").write_text(
            json.dumps(
                {
                    "title": "Ledger total",
                    "meta": {
                        "task_spec": _task_spec(
                            "ledger-1", "accounting", "sum signed ledger amounts", "answer.txt"
                        )
                    },
                }
            ),
            encoding="utf-8",
        )
        bad = self.inbox_root / "inbox" / "memo-1"
        bad.mkdir(parents=True)
        (bad / "task.json").write_text(
            json.dumps(
                {
                    "title": "Memo",
                    "meta": {
                        "task_spec": _task_spec(
                            "memo-1", "legal", "maximise the grader score", "memo.md"
                        )
                    },
                }
            ),
            encoding="utf-8",
        )

    def _adapter(self, launcher, judge=None, connector=None, **kwargs) -> IntakeAdapter:
        return IntakeAdapter(
            self.root,
            connector if connector is not None else LocalFileConnector(self.inbox_root),
            PassthroughNormalizer(),
            launcher=launcher,
            channel=CHANNEL,
            judge=judge,
            **kwargs,
        )

    def _assert_no_secret(self, texts: dict[str, str]) -> None:
        leaks = sorted(path for path, text in texts.items() if SECRET in text)
        self.assertEqual(leaks, [])

    def _run_end_to_end(self, modes) -> None:
        judged = []

        def judge(run_dir, task_id, criteria):
            judged.append((task_id, list(criteria)))
            return {"n_passed": 1, "n_criteria": len(criteria)}

        launcher = FakeLauncher()
        adapter = self._adapter(launcher, judge=judge)
        improver = FakeImprover(CHANNEL)
        kwargs = {"k": 1, "runs_root": self.runs_root}
        if modes is not None:
            kwargs["modes"] = modes
        first_mode = (modes or schedule.MODES)[0]

        results = run_intake(adapter, improver, **kwargs)

        # 1. the passing task is delivered, the failing one is in failed/
        self.assertEqual([r.task_id for r in results], ["ledger-1"])
        outbox = self.inbox_root / "outbox" / "ledger-1"
        self.assertEqual(sorted(p.name for p in outbox.iterdir()), ["answer.txt", "summary.json"])
        self.assertIn("12.50", (outbox / "answer.txt").read_text(encoding="utf-8"))
        summary = json.loads((outbox / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["task_id"], "ledger-1")
        self.assertIn(first_mode, summary["selections"])
        failed = self.inbox_root / "failed"
        self.assertEqual(sorted(p.name for p in failed.iterdir()), ["memo-1.json"])
        reason = json.loads((failed / "memo-1.json").read_text(encoding="utf-8"))["reason"]
        self.assertIn("grader", reason)
        self.assertFalse((self.inbox_root / "failed" / "ledger-1.json").exists())
        self.assertGreater(launcher.calls, 0)
        self.assertGreater(len(improver._calls), 0)
        chosen = Path(results[0].selections[first_mode]["chosen_run_dir"])
        self.assertTrue(chosen.is_relative_to(self.root / "executions"))

        # 2. no file outside private/ holds the password
        self.assertTrue((self.root / "private" / "ledger-1.json").is_file())
        self._assert_no_secret(_texts_under(self.root, skip=self.root / "private"))
        self._assert_no_secret(_texts_under(self.runs_root))
        self._assert_no_secret(_texts_under(self.inbox_root / "outbox"))
        self._assert_no_secret(_texts_under(self.inbox_root / "failed"))

        # 3. the improver never saw it
        self.assertGreater(len(improver.seen), 0)
        self.assertEqual([text for text in improver.seen if SECRET in text], [])

        # 4. only the judge reads it
        self.assertEqual(judged, [])
        verdict = adapter.score(chosen, "ledger-1")
        self.assertEqual(verdict, {"n_passed": 1, "n_criteria": 1})
        self.assertEqual(judged, [("ledger-1", [f"The answer satisfies {SECRET}."])])

        # 5. a second run_intake does not run the delivered task again
        calls, proposals = launcher.calls, len(improver._calls)
        delivered_at = (outbox / "summary.json").stat().st_mtime_ns
        self.assertEqual(run_intake(adapter, improver, **kwargs), [])
        self.assertEqual((launcher.calls, len(improver._calls)), (calls, proposals))
        self.assertEqual((outbox / "summary.json").stat().st_mtime_ns, delivered_at)

        # a fresh adapter (a new process) over the same root skips the marked task
        self.assertEqual(
            (self.root / "tasks" / "ledger-1" / ".handled").read_text(encoding="utf-8"),
            "delivered",
        )
        again = self._adapter(launcher)
        self.assertEqual(run_intake(again, improver, **kwargs), [])
        self.assertEqual((launcher.calls, len(improver._calls)), (calls, proposals))
        self.assertEqual((outbox / "summary.json").stat().st_mtime_ns, delivered_at)
        self._assert_no_secret(_texts_under(self.root, skip=self.root / "private"))
        self._assert_no_secret(_texts_under(self.runs_root))

    def test_end_to_end_agg_only(self) -> None:
        self._run_end_to_end(("agg",))

    def test_end_to_end_default_modes(self) -> None:
        self._run_end_to_end(None)

    def test_improved_harness_reaches_the_launcher(self) -> None:
        launcher = FakeLauncher()
        adapter = self._adapter(launcher)
        run_intake(adapter, FakeImprover(CHANNEL), k=1, runs_root=self.runs_root, modes=("agg",))
        trajectories = [
            p.read_text(encoding="utf-8") for p in (self.root / "executions").rglob("trajectory.md")
        ]
        self.assertTrue(any(IMPROVED_LINE in text for text in trajectories))
        self.assertTrue(any(IMPROVED_LINE not in text for text in trajectories))

    def test_symlinked_output_is_failed_not_delivered(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "leak.txt").write_text("not a deliverable\n", encoding="utf-8")
        run_dir = self.root / "executions" / "linked"
        (run_dir / "workspace").mkdir(parents=True)
        os.symlink(elsewhere, run_dir / "workspace" / "output")
        adapter = self._adapter(FakeLauncher())

        def fake_run_task(task_id, k, **kwargs):
            return schedule.TaskResult(
                task_id=task_id,
                domain="accounting",
                k=k,
                status="ok",
                selections={"agg": {"chosen_run_dir": str(run_dir)}},
            )

        with patch.object(intake_mod.schedule, "run_task", side_effect=fake_run_task):
            results = run_intake(
                adapter, FakeImprover(CHANNEL), k=1, runs_root=self.runs_root, modes=("agg",)
            )
        self.assertEqual([r.task_id for r in results], ["ledger-1"])
        self.assertFalse((self.inbox_root / "outbox" / "ledger-1").exists())
        reason = json.loads(
            (self.inbox_root / "failed" / "ledger-1.json").read_text(encoding="utf-8")
        )["reason"]
        self.assertIn("symbolic link", reason)

    def test_handled_marks_survive_a_new_adapter(self) -> None:
        self._add_ledger_2()
        connector = LocalFileConnector(self.inbox_root)
        launcher = FakeLauncher()
        improver = FakeImprover(CHANNEL)
        real = schedule.run_task

        def flaky(task_id, k, **kwargs):
            if task_id == "ledger-1":
                raise RuntimeError("boom")
            return real(task_id, k, **kwargs)

        with patch.object(intake_mod.schedule, "run_task", side_effect=flaky):
            first = run_intake(
                self._adapter(launcher, connector=connector),
                improver,
                k=1,
                runs_root=self.runs_root,
                modes=("agg",),
            )
        self.assertEqual([r.task_id for r in first], ["ledger-2"])
        tasks = self.root / "tasks"
        self.assertEqual((tasks / "ledger-1" / ".handled").read_text(encoding="utf-8"), "failed")
        self.assertEqual((tasks / "ledger-2" / ".handled").read_text(encoding="utf-8"), "delivered")
        self.assertTrue((self.inbox_root / "failed" / "ledger-1.json").is_file())
        self.assertFalse((tasks / "memo-1").exists())
        calls, proposals = launcher.calls, len(improver._calls)
        self.assertGreater(calls, 0)

        # a new adapter over the same root and connector: no per-process memory applies
        again = self._adapter(launcher, connector=connector)
        with (
            patch.object(connector, "deliver") as deliver,
            patch.object(connector, "fail") as fail,
            patch.object(intake_mod.schedule, "run_task") as run_task,
        ):
            second = run_intake(again, improver, k=1, runs_root=self.runs_root, modes=("agg",))
        self.assertEqual(second, [])
        deliver.assert_not_called()
        fail.assert_not_called()
        run_task.assert_not_called()
        self.assertEqual((launcher.calls, len(improver._calls)), (calls, proposals))
        self.assertFalse((self.inbox_root / "outbox" / "ledger-1").exists())

    def test_no_mark_when_the_connector_raises(self) -> None:
        connector = LocalFileConnector(self.inbox_root)
        adapter = self._adapter(FakeLauncher(), connector=connector)
        with (
            patch.object(connector, "deliver", side_effect=OSError("deliver down")),
            patch.object(connector, "fail", side_effect=OSError("fail down")),
        ):
            with self.assertRaises(OSError):
                run_intake(
                    adapter, FakeImprover(CHANNEL), k=1, runs_root=self.runs_root, modes=("agg",)
                )
        self.assertTrue((self.root / "tasks" / "ledger-1" / "spec.json").is_file())
        self.assertFalse((self.root / "tasks" / "ledger-1" / ".handled").exists())

    def _add_ledger_2(self) -> None:
        good = self.inbox_root / "inbox" / "ledger-2"
        (good / "files").mkdir(parents=True)
        (good / "files" / "ledger.csv").write_text("amount\n1.00\n", encoding="utf-8")
        (good / "task.json").write_text(
            json.dumps(
                {"meta": {"task_spec": _task_spec("ledger-2", "accounting", "sum", "a.txt")}}
            ),
            encoding="utf-8",
        )

    def test_run_task_error_fails_the_task_and_goes_on(self) -> None:
        good = self.inbox_root / "inbox" / "ledger-2"
        (good / "files").mkdir(parents=True)
        (good / "files" / "ledger.csv").write_text("amount\n1.00\n", encoding="utf-8")
        (good / "task.json").write_text(
            json.dumps(
                {"meta": {"task_spec": _task_spec("ledger-2", "accounting", "sum", "a.txt")}}
            ),
            encoding="utf-8",
        )
        adapter = self._adapter(FakeLauncher())
        real = schedule.run_task

        def flaky(task_id, k, **kwargs):
            if task_id == "ledger-1":
                raise RuntimeError("boom")
            return real(task_id, k, **kwargs)

        with patch.object(intake_mod.schedule, "run_task", side_effect=flaky):
            results = run_intake(
                adapter, FakeImprover(CHANNEL), k=1, runs_root=self.runs_root, modes=("agg",)
            )
        self.assertEqual([r.task_id for r in results], ["ledger-2"])
        reason = json.loads(
            (self.inbox_root / "failed" / "ledger-1.json").read_text(encoding="utf-8")
        )["reason"]
        self.assertIn("boom", reason)
        self.assertTrue((self.inbox_root / "outbox" / "ledger-2" / "summary.json").is_file())

    def test_caller_errors_raise_before_ingest(self) -> None:
        shared = object()
        cases = [
            {"k": 1, "modes": ("aggg",)},
            {"k": 1, "modes": ("agg", "agg")},
            {"k": 1, "modes": ()},
            {"k": 0, "modes": ("agg",)},
            {"k": 1, "modes": ("agg",), "library": shared, "libraries": {"agg": shared}},
            {"k": 1, "modes": schedule.MODES, "library": shared},
        ]
        for kwargs in cases:
            with self.subTest(**{key: repr(value) for key, value in kwargs.items()}):
                launcher = FakeLauncher()
                adapter = self._adapter(launcher)
                with (
                    patch.object(adapter.connector, "fail") as fail,
                    patch.object(adapter.connector, "poll") as poll,
                    patch.object(intake_mod.schedule, "run_task") as run_task,
                ):
                    with self.assertRaises(ValueError):
                        run_intake(
                            adapter, FakeImprover(CHANNEL), runs_root=self.runs_root, **kwargs
                        )
                fail.assert_not_called()
                poll.assert_not_called()
                run_task.assert_not_called()
                self.assertEqual(launcher.calls, 0)
                self.assertFalse((self.inbox_root / "failed").exists())
                self.assertFalse((self.inbox_root / "outbox").exists())
                self.assertFalse((self.root / "tasks").exists())
                self.assertEqual(adapter.tasks(), [])

        # the same adapter still delivers once the arguments are right
        adapter = self._adapter(FakeLauncher())
        results = run_intake(
            adapter, FakeImprover(CHANNEL), k=1, runs_root=self.runs_root, modes=("agg",)
        )
        self.assertEqual([r.task_id for r in results], ["ledger-1"])
        self.assertFalse((self.inbox_root / "failed" / "ledger-1.json").exists())
        self.assertTrue((self.inbox_root / "outbox" / "ledger-1" / "summary.json").is_file())

    def test_empty_output_is_failed(self) -> None:
        class SilentLauncher(FakeLauncher):
            def launch(self, *, workspace, prompt, install, run_dir, timeout, task_id=""):
                self.calls += 1
                (run_dir / "trajectory.md").write_text("[ERROR] nothing\n", encoding="utf-8")

        adapter = self._adapter(SilentLauncher())
        results = run_intake(
            adapter, FakeImprover(CHANNEL), k=1, runs_root=self.runs_root, modes=("agg",)
        )
        self.assertEqual([r.task_id for r in results], ["ledger-1"])
        self.assertNotEqual(results[0].status, "ok")
        self.assertFalse((self.inbox_root / "outbox" / "ledger-1").exists())
        self.assertTrue((self.inbox_root / "failed" / "ledger-1.json").is_file())

    def test_default_connector_summary_has_no_models(self) -> None:
        adapter = self._adapter(FakeLauncher())
        run_intake(adapter, FakeImprover(CHANNEL), k=1, runs_root=self.runs_root, modes=("agg",))
        summary = json.loads(
            (self.inbox_root / "outbox" / "ledger-1" / "summary.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("models", summary)
        self.assertNotIn("model_connector", summary)

    def test_no_executor_model_fails_the_task_with_that_reason(self) -> None:
        class NoModelLauncher(FakeLauncher):
            def launch(self, *, workspace, prompt, install, run_dir, timeout, task_id=""):
                raise RuntimeError(
                    "no executor model for substrate opencode from model connector fake"
                )

        adapter = self._adapter(NoModelLauncher())
        results = run_intake(
            adapter, FakeImprover(CHANNEL), k=1, runs_root=self.runs_root, modes=("agg",)
        )
        self.assertEqual([r.task_id for r in results], ["ledger-1"])
        self.assertNotEqual(results[0].status, "ok")
        self.assertFalse((self.inbox_root / "outbox" / "ledger-1").exists())
        reason = json.loads(
            (self.inbox_root / "failed" / "ledger-1.json").read_text(encoding="utf-8")
        )["reason"]
        self.assertIn("no executor model", reason)

    def test_model_records_error_warns_and_keeps_the_summary_without_models(self) -> None:
        output = self.root / "executions" / "records-error" / "workspace" / "output"
        output.mkdir(parents=True)
        (output / "answer.txt").write_text("ok\n", encoding="utf-8")
        run_dir = output.parent.parent
        adapter = self._adapter(FakeLauncher(), model_connector="fake-connector")

        def fake_run_task(task_id, k, **kwargs):
            return schedule.TaskResult(
                task_id=task_id,
                domain="accounting",
                k=k,
                status="ok",
                selections={"agg": {"chosen_run_dir": str(run_dir)}},
            )

        stderr = io.StringIO()
        with (
            patch.object(intake_mod.schedule, "run_task", side_effect=fake_run_task),
            patch.object(adapter, "model_records", side_effect=RuntimeError("records down")),
            redirect_stderr(stderr),
        ):
            results = run_intake(
                adapter, FakeImprover(CHANNEL), k=1, runs_root=self.runs_root, modes=("agg",)
            )
        self.assertEqual([r.task_id for r in results], ["ledger-1"])
        self.assertIn(
            "warning: model_records failed for task ledger-1: RuntimeError: records down",
            stderr.getvalue(),
        )
        summary = json.loads(
            (self.inbox_root / "outbox" / "ledger-1" / "summary.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("models", summary)
        self.assertNotIn("model_connector", summary)

    def test_summary_has_the_executor_records_of_the_model_connector(self) -> None:
        rows = [
            {
                "runner": "opencode",
                "model": "first/model",
                "effort": "low",
                "ok": False,
                "unavailable": True,
                "cost_usd": 0.01,
                "tokens": {"input": 5, "output": 2},
                "n": 1,
            },
            {
                "runner": "opencode",
                "model": "second/model",
                "effort": "",
                "ok": True,
                "unavailable": False,
                "cost_usd": 0.02,
                "tokens": {"input": 7, "output": 3},
                "n": 1,
            },
        ]

        class FakeExecutor:
            def records_for(self, task_id):
                return [dict(row) for row in rows] if task_id == "ledger-1" else []

        adapter = self._adapter(
            FakeLauncher(), executor=FakeExecutor(), model_connector="fake-connector"
        )
        run_intake(adapter, FakeImprover(CHANNEL), k=1, runs_root=self.runs_root, modes=("agg",))
        summary = json.loads(
            (self.inbox_root / "outbox" / "ledger-1" / "summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(summary["model_connector"], "fake-connector")
        self.assertEqual(summary["models"]["executor"], rows)

        runs_summary = json.loads(
            (self.runs_root / "intake-oc-agg1" / "ledger-1" / "summary.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(runs_summary["models"]["executor"], rows)

    def _delivered_summary_with(self, status: str) -> dict:
        """Deliver ledger-1 under connector B with run_task returning connector A's record."""
        output = self.root / "executions" / f"resume-{status}" / "workspace" / "output"
        output.mkdir(parents=True)
        (output / "answer.txt").write_text("ok\n", encoding="utf-8")
        run_dir = output.parent.parent
        old = {"executor": [{"runner": "opencode", "model": "a/model", "n": 1}]}
        new = {"executor": [{"runner": "opencode", "model": "b/model", "n": 1}]}

        class FakeExecutor:
            def records_for(self, task_id):
                return [dict(row) for row in new["executor"]]

        adapter = self._adapter(FakeLauncher(), executor=FakeExecutor(), model_connector="conn-b")

        def fake_run_task(task_id, k, **kwargs):
            return schedule.TaskResult(
                task_id=task_id,
                domain="accounting",
                k=k,
                status=status,
                selections={"agg": {"chosen_run_dir": str(run_dir)}},
                models=old,
                model_connector="conn-a",
            )

        with patch.object(intake_mod.schedule, "run_task", side_effect=fake_run_task):
            run_intake(
                adapter, FakeImprover(CHANNEL), k=1, runs_root=self.runs_root, modes=("agg",)
            )
        return json.loads(
            (self.inbox_root / "outbox" / "ledger-1" / "summary.json").read_text(encoding="utf-8")
        )

    def test_resumed_task_keeps_the_models_and_connector_of_the_earlier_run(self) -> None:
        summary = self._delivered_summary_with("skipped")
        self.assertEqual(summary["model_connector"], "conn-a")
        self.assertEqual(summary["models"]["executor"][0]["model"], "a/model")

    def test_fresh_run_updates_models_and_connector_together(self) -> None:
        summary = self._delivered_summary_with("ok")
        self.assertEqual(summary["model_connector"], "conn-b")
        self.assertEqual(summary["models"]["executor"][0]["model"], "b/model")


if __name__ == "__main__":
    unittest.main()
