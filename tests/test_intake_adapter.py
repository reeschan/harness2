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

"""The intake adapter of benchmarks.intake: ingest, isolation of private criteria, rollouts."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness2.benchmarks.intake import IntakeAdapter, Launcher
from harness2.core import spec
from harness2.core.intake import RawTask
from harness2.core.proposer import Proposer

SECRET = "SECRET-CRITERION-7f3a"
CHANNEL = spec.ChannelSpec("opencode", "fake-model")


def _spec_dict(task_id: str, domain: str = "accounting", **extra) -> dict:
    out = {
        "task_id": task_id,
        "domain": domain,
        "prompt": f"Summarise the ledger for {task_id}.",
        "files": [],
        "deliverable": {"what": "a short summary", "where": "summary.txt"},
        "prompts": {"work": "summarise ledgers"},
        "private_criteria": [SECRET],
    }
    out.update(extra)
    return out


class FakeConnector:
    def __init__(self, raws=(), files=None, fetch_error=None):
        self.raws = list(raws)
        self.files = files or {}
        self.fetch_error = fetch_error
        self.failed: list[tuple[str, str]] = []
        self.delivered: list[str] = []

    def poll(self):
        return list(self.raws)

    def fetch_files(self, raw, dest):
        if self.fetch_error is not None:
            raise self.fetch_error
        for rel, text in self.files.get(raw.id, {}).items():
            path = Path(dest) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def deliver(self, task_id, deliverable_dir, summary):
        self.delivered.append(task_id)

    def fail(self, task_id, reason):
        self.failed.append((task_id, reason))


class FakeNormalizer:
    """Returns `answers[raw.id]` (a dict, or an exception to raise); records each call."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def normalize(self, raw, *, domains, feedback=""):
        self.calls.append((raw.id, tuple(domains)))
        answer = self.answers[raw.id]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeLauncher:
    def __init__(self, error=None):
        self.calls: list[dict] = []
        self.error = error

    def launch(self, *, workspace, prompt, install, run_dir, timeout, task_id=""):
        self.calls.append(
            {
                "workspace": workspace,
                "prompt": prompt,
                "install": install,
                "run_dir": run_dir,
                "timeout": timeout,
                "task_id": task_id,
                "staging_exists": install is not None and install.staging_dir.is_dir(),
                "workspace_files": sorted(
                    p.relative_to(workspace).as_posix() for p in workspace.rglob("*")
                ),
            }
        )
        if self.error is not None:
            raise self.error
        (workspace / "output" / "answer.txt").write_text("Total: 1.00\n", encoding="utf-8")
        (run_dir / "trajectory.md").write_text(
            "[ACTION] sum\n[OBSERVATION]\nTotal: 1.00\n", encoding="utf-8"
        )


class IntakeAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"

    def _adapter(self, raws, answers, *, files=None, launcher=None, judge=None, domains=(), **kw):
        self.conn = FakeConnector(raws, files=files, **kw)
        self.norm = FakeNormalizer(answers)
        self.launcher = launcher or FakeLauncher()
        return IntakeAdapter(
            self.root,
            self.conn,
            self.norm,
            launcher=self.launcher,
            channel=CHANNEL,
            judge=judge,
            domains=domains,
        )

    def _one(self, **kw) -> IntakeAdapter:
        adapter = self._adapter(
            [RawTask("t1")],
            {"t1": _spec_dict("t1")},
            files={"t1": {"ledger.csv": "amount\n1.00\n", "private_criteria.txt": SECRET}},
            **kw,
        )
        self.assertEqual(adapter.ingest(), ["t1"])
        return adapter

    def _all_text(self, base: Path, *, skip: Path | None = None) -> str:
        parts = []
        for p in base.rglob("*"):
            if p.is_file() and (skip is None or skip not in p.parents):
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts)

    # -- ingest ----------------------------------------------------------------------------

    def test_ingest_stores_spec_files_and_private(self) -> None:
        adapter = self._one()
        stored = json.loads((self.root / "tasks/t1/spec.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["task_id"], "t1")
        self.assertNotIn("private_criteria", stored)
        self.assertTrue((self.root / "tasks/t1/files/ledger.csv").is_file())
        private = json.loads((self.root / "private/t1.json").read_text(encoding="utf-8"))
        self.assertEqual(private, {"private_criteria": [SECRET]})
        self.assertEqual(adapter.tasks(), ["t1"])
        self.assertEqual(adapter.domain_of("t1"), "accounting")
        self.assertEqual(self.conn.failed, [])

    def test_ingest_normalize_error_fails_the_task(self) -> None:
        adapter = self._adapter(
            [RawTask("bad"), RawTask("good")],
            {"bad": {"task_id": "bad"}, "good": _spec_dict("good")},
        )
        self.assertEqual(adapter.ingest(), ["good"])
        self.assertEqual([t for t, _ in self.conn.failed], ["bad"])
        self.assertIn("missing the required key", self.conn.failed[0][1])
        self.assertFalse((self.root / "tasks/bad").exists())

    def test_ingest_is_idempotent(self) -> None:
        adapter = self._one()
        calls = len(self.norm.calls)
        self.assertEqual(adapter.ingest(), [])
        self.assertEqual(len(self.norm.calls), calls)
        self.assertEqual(adapter.tasks(), ["t1"])

    def test_known_domains_reach_the_second_ingest(self) -> None:
        adapter = self._adapter(
            [RawTask("t1")], {"t1": _spec_dict("t1", "tax")}, domains=("law", "tax")
        )
        adapter.ingest()
        self.assertEqual(self.norm.calls[0][1], ("law", "tax"))
        self.conn.raws = [RawTask("t2")]
        self.norm.answers["t2"] = _spec_dict("t2", "audit")
        adapter.domains = ("law",)
        adapter.ingest()
        self.assertEqual(self.norm.calls[-1], ("t2", ("law", "tax")))
        self.assertEqual(adapter.tasks(), ["t1", "t2"])

    def test_ingest_rejects_a_task_id_that_differs_from_the_raw_id(self) -> None:
        adapter = self._adapter([RawTask("t1")], {"t1": _spec_dict("other")})
        self.assertEqual(adapter.ingest(), [])
        self.assertEqual([t for t, _ in self.conn.failed], ["t1"])
        self.assertIn("other", self.conn.failed[0][1])
        self.assertEqual(adapter.tasks(), [])
        self.assertFalse((self.root / "tasks/other").exists())

    def test_ingest_fails_only_the_task_whose_fetch_raises(self) -> None:
        adapter = self._adapter(
            [RawTask("t1")], {"t1": _spec_dict("t1")}, fetch_error=OSError("disk gone")
        )
        self.assertEqual(adapter.ingest(), [])
        self.assertEqual(self.conn.failed[0][0], "t1")
        self.assertIn("disk gone", self.conn.failed[0][1])
        self.assertFalse((self.root / "tasks/t1").exists())
        self.conn.fetch_error = None
        self.assertEqual(adapter.ingest(), ["t1"])

    def test_ingest_lets_base_exceptions_through(self) -> None:
        adapter = self._adapter(
            [RawTask("t1")], {"t1": _spec_dict("t1")}, fetch_error=KeyboardInterrupt()
        )
        with self.assertRaises(KeyboardInterrupt):
            adapter.ingest()
        self.assertEqual(self.conn.failed, [])

    def test_fail_reason_is_clipped(self) -> None:
        huge = "x" * 50_000
        adapter = self._adapter([RawTask("t1")], {"t1": _spec_dict("t1", prompt="", domain=huge)})
        adapter.ingest()
        self.assertEqual(len(self.conn.failed), 1)
        self.assertLessEqual(len(self.conn.failed[0][1]), 2_000)

    def test_tasks_accepts_only_all(self) -> None:
        adapter = self._one()
        with self.assertRaises(ValueError):
            adapter.tasks("train")

    # -- private criteria ------------------------------------------------------------------

    def test_private_criteria_stay_out_of_everything_the_agent_sees(self) -> None:
        adapter = self._one()
        self.assertFalse((self.root / "tasks/t1/files/private_criteria.txt").exists())
        self.assertNotIn(SECRET, self._all_text(self.root / "tasks"))
        self.assertNotIn(SECRET, adapter.task_prompt("t1"))
        self.assertNotIn(SECRET, repr(adapter.prompts_for("t1")))
        run_dir = adapter.execute("t1", None, "r1")
        self.assertNotIn("private_criteria.txt", self.launcher.calls[0]["workspace_files"])
        self.assertNotIn(SECRET, self.launcher.calls[0]["prompt"])
        self.assertNotIn(SECRET, self._all_text(run_dir))
        self.assertNotIn(SECRET, self._all_text(self.root, skip=self.root / "private"))

    def test_score_hands_private_criteria_to_the_judge(self) -> None:
        seen = []

        def judge(run_dir, task_id, criteria):
            seen.append((run_dir, task_id, list(criteria)))
            return {"passed": True}

        adapter = self._one(judge=judge)
        run_dir = adapter.execute("t1", None, "r1")
        self.assertEqual(adapter.score(run_dir, "t1"), {"passed": True})
        self.assertEqual(seen, [(run_dir, "t1", [SECRET])])

    def test_score_without_judge_is_none(self) -> None:
        adapter = self._one()
        run_dir = adapter.execute("t1", None, "r1")
        self.assertIsNone(adapter.score(run_dir, "t1"))

    # -- the BenchmarkAdapter surface ------------------------------------------------------

    def test_prompt_names_the_deliverable_location(self) -> None:
        adapter = self._one()
        text = adapter.task_prompt("t1")
        self.assertTrue(text.startswith("Summarise the ledger for t1."))
        self.assertIn("a short summary", text)
        self.assertIn("output/summary.txt", text)
        self.assertEqual(adapter.prompts_for("t1").work, "summarise ledgers")
        self.assertEqual(adapter.task_files("t1"), self.root / "tasks/t1/files")

    def test_task_files_none_without_files_dir(self) -> None:
        adapter = self._one()
        (self.root / "tasks/t1/files/ledger.csv").unlink()
        (self.root / "tasks/t1/files").rmdir()
        self.assertIsNone(adapter.task_files("t1"))
        run_dir = adapter.execute("t1", None, "r1")
        self.assertTrue((run_dir / "workspace/output").is_dir())

    def _base(self, base_tree):
        return IntakeAdapter(
            self.root,
            FakeConnector([]),
            FakeNormalizer({}),
            launcher=FakeLauncher(),
            channel=CHANNEL,
            base_tree=base_tree,
        )

    def test_base_tree_replaces_the_always_on_files(self) -> None:
        adapter = self._base({"systemprompt.md": "Follow worker.md.\n", "guardrails.md": "Rule.\n"})
        base = adapter.base_harness()
        self.assertEqual((base / "systemprompt.md").read_text(), "Follow worker.md.\n")
        self.assertEqual((base / "guardrails.md").read_text(), "Rule.\n")

    def test_without_base_tree_the_default_system_prompt_stays(self) -> None:
        adapter = self._base(None)
        text = (adapter.base_harness() / "systemprompt.md").read_text()
        self.assertIn("output/", text)

    def test_base_tree_rejects_other_files_and_the_cap(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._base({"skills/x/SKILL.md": "no"})
        self.assertIn("not always-on", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self._base({"systemprompt.md": "x" * 30_000})
        self.assertIn("cap", str(ctx.exception))

    def test_channel_is_required(self) -> None:
        with self.assertRaises(TypeError):
            IntakeAdapter(self.root, FakeConnector([]), FakeNormalizer({}), launcher=FakeLauncher())

    def test_empty_model_channel_is_rejected(self) -> None:
        for model in ("", "   "):
            with self.subTest(model=model), self.assertRaises(ValueError):
                IntakeAdapter(
                    self.root,
                    FakeConnector([]),
                    FakeNormalizer({}),
                    launcher=FakeLauncher(),
                    channel=spec.ChannelSpec("opencode", model),
                )

    def test_channel_is_used_for_proposer_and_selector(self) -> None:
        adapter = self._one()
        self.assertIs(adapter.spec.channels["proposer"], CHANNEL)
        self.assertIs(adapter.spec.channels["selector"], CHANNEL)
        Proposer(adapter.spec.channels["selector"])

    def test_base_harness_and_surface(self) -> None:
        adapter = self._one()
        base = adapter.base_harness()
        for name in adapter.substrate.surface().always_on:
            self.assertTrue((base / name).is_file(), name)
        self.assertIn("output/", (base / "systemprompt.md").read_text(encoding="utf-8"))
        self.assertIsInstance(adapter.substrate, spec.SubstrateLike)
        self.assertEqual(adapter.spec.name, "intake")
        self.assertIn("private/**", adapter.spec.leak_globs)
        self.assertIn("private_criteria*", adapter.spec.leak_globs)
        for name in (
            "tasks",
            "domain_of",
            "task_prompt",
            "task_files",
            "execute",
            "delivered",
            "trajectory",
            "deliverable_files",
            "deliverable_text",
            "base_harness",
            "validate_tree",
            "score",
        ):
            self.assertTrue(callable(getattr(adapter, name)), name)
        self.assertIsInstance(adapter, spec.BenchmarkAdapter)
        self.assertIsInstance(self.launcher, Launcher)
        self.assertIsNone(adapter.validate_tree({"systemprompt.md": "anything"}))
        self.assertIsInstance(spec.resolve_prompts(adapter, "t1"), spec.BenchPrompts)

    # -- execute ---------------------------------------------------------------------------

    def test_execute_is_idempotent(self) -> None:
        adapter = self._one()
        first = adapter.execute("t1", None, "r1", timeout=12)
        second = adapter.execute("t1", None, "r1")
        self.assertEqual(first, second)
        self.assertEqual(len(self.launcher.calls), 1)
        self.assertEqual(self.launcher.calls[0]["timeout"], 12)
        self.assertTrue((first / ".complete").is_file())

    def test_execute_base_harness_has_no_install(self) -> None:
        adapter = self._one()
        run_dir = adapter.execute("t1", None, "r1")
        call = self.launcher.calls[0]
        self.assertIsNone(call["install"])
        self.assertEqual(call["timeout"], 3600)
        self.assertEqual(call["run_dir"], run_dir)
        self.assertEqual(call["workspace"], run_dir / "workspace")
        self.assertIn("ledger.csv", call["workspace_files"])
        self.assertIn("output", call["workspace_files"])

    def test_execute_with_harness_materialises_and_cleans_up(self) -> None:
        adapter = self._one()
        adapter.execute("t1", adapter.base_harness(), "r2")
        call = self.launcher.calls[0]
        self.assertIsNotNone(call["install"])
        self.assertTrue(call["staging_exists"])
        self.assertFalse(call["install"].staging_dir.exists())

    def test_launcher_error_leaves_no_complete_mark(self) -> None:
        adapter = self._one(launcher=FakeLauncher(error=OSError("boom")))
        with self.assertRaises(RuntimeError) as ctx:
            adapter.execute("t1", adapter.base_harness(), "r1")
        self.assertIn("boom", str(ctx.exception))
        run_dir = self.root / "executions/r1"
        self.assertFalse((run_dir / ".complete").exists())
        self.assertFalse(self.launcher.calls[0]["install"].staging_dir.exists())
        self.launcher.error = None
        adapter.execute("t1", None, "r1")
        self.assertEqual(len(self.launcher.calls), 2)
        self.assertTrue((run_dir / ".complete").is_file())

    def test_deliverables_and_trajectory(self) -> None:
        adapter = self._one()
        run_dir = adapter.execute("t1", None, "r1")
        self.assertTrue(adapter.delivered(run_dir))
        self.assertEqual(adapter.deliverable_files(run_dir), {"answer.txt": "Total: 1.00\n"})
        self.assertEqual(adapter.deliverable_text(run_dir), "=== answer.txt ===\nTotal: 1.00\n")
        self.assertIn("[ACTION] sum", adapter.trajectory(run_dir, obs_trunc=50_000))
        self.assertEqual(len(adapter.trajectory(run_dir, obs_trunc=2)), 8)

    def test_deliverable_files_cap_and_bad_bytes(self) -> None:
        adapter = self._one()
        run_dir = adapter.execute("t1", None, "r1")
        output = run_dir / "workspace/output"
        (output / "big.txt").write_text("a" * 70_000, encoding="utf-8")
        (output / "sub").mkdir()
        (output / "sub/bin.dat").write_bytes(b"ok\xff")
        files = adapter.deliverable_files(run_dir)
        self.assertEqual(len(files["big.txt"]), 60_000)
        self.assertEqual(files["sub/bin.dat"], "ok�")

    def test_nothing_delivered(self) -> None:
        adapter = self._one()
        run_dir = self.root / "executions/empty"
        (run_dir / "workspace/output").mkdir(parents=True)
        self.assertFalse(adapter.delivered(run_dir))
        self.assertEqual(adapter.deliverable_files(run_dir), {})
        self.assertEqual(
            adapter.trajectory(run_dir, obs_trunc=10_000), "[ERROR] no trajectory recorded"
        )

    def test_symlinked_output_is_not_followed(self) -> None:
        adapter = self._one()
        run_dir = adapter.execute("t1", None, "r1")
        output = run_dir / "workspace/output"
        os.symlink(self.root / "private/t1.json", output / "leak.json")
        os.symlink(self.root / "private", output / "leakdir")
        files = adapter.deliverable_files(run_dir)
        self.assertEqual(sorted(files), ["answer.txt"])
        self.assertNotIn(SECRET, adapter.deliverable_text(run_dir))

        other = self.root / "executions/linked"
        (other / "workspace").mkdir(parents=True)
        os.symlink(output, other / "workspace/output")
        self.assertFalse(adapter.delivered(other))
        self.assertEqual(adapter.deliverable_files(other), {})

    def _links_under(self, base: Path) -> list[str]:
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_symlink())

    def test_attachment_symlinks_are_dropped_at_ingest(self) -> None:
        adapter = self._adapter(
            [RawTask("victim"), RawTask("evil")],
            {"victim": _spec_dict("victim"), "evil": _spec_dict("evil")},
            files={"victim": {"a.txt": "a\n"}, "evil": {"a.txt": "a\n"}},
        )
        plain_fetch = self.conn.fetch_files

        def evil_fetch(raw, dest):
            plain_fetch(raw, dest)
            if raw.id == "evil":
                os.symlink("../../../private", Path(dest) / "notes")
                os.symlink("../../../private/victim.json", Path(dest) / "ref.json")
                os.mkfifo(Path(dest) / "pipe")

        self.conn.fetch_files = evil_fetch
        self.assertEqual(adapter.ingest(), ["victim", "evil"])
        files = self.root / "tasks/evil/files"
        self.assertEqual(self._links_under(files), [])
        self.assertEqual(sorted(p.name for p in files.iterdir()), ["a.txt"])

        run_dir = adapter.execute("evil", None, "r1")
        workspace = run_dir / "workspace"
        self.assertEqual(self._links_under(workspace), [])
        self.assertNotIn(SECRET, self._all_text(workspace))
        self.assertEqual(self.launcher.calls[0]["workspace_files"], ["a.txt", "output"])

    def test_execute_does_not_follow_links_placed_after_ingest(self) -> None:
        adapter = self._one()
        files = self.root / "tasks/t1/files"
        os.symlink("../../../private", files / "notes")
        os.symlink(self.root / "private/t1.json", files / "ref.json")
        (files / "sub").mkdir()
        os.symlink("../../../../private/t1.json", files / "sub/deep.json")
        run_dir = adapter.execute("t1", None, "r1")
        workspace = run_dir / "workspace"
        self.assertEqual(self._links_under(workspace), [])
        self.assertNotIn(SECRET, self._all_text(workspace))
        self.assertEqual(self.launcher.calls[0]["workspace_files"], ["ledger.csv", "output", "sub"])


if __name__ == "__main__":
    unittest.main()
