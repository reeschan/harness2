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

"""Recall injection through run_task / run_domain: per-mode text, the summary record, resume."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_connectors import InMemoryConnector

from harness2.core import library, memory, recall, schedule
from harness2.core import tree as harness_tree
from harness2.demo import DemoAdapter, DemoProposer

DOMAIN = "accounting"
K = 1
LESSON = "Include both signs when summing in scripts/summarize.py."
CONNECTOR = "memory_connectors:InMemoryConnector"
VERDICT = "recall_run_test_verdict"


class _VerdictConnector(InMemoryConnector):
    def recall(self, task_id, limit_chars):
        return "deterministic_eval_summary: passed"


class MemoryRecallRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        InMemoryConnector.reset()
        self.addCleanup(InMemoryConnector.reset)
        env = patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(memory.ENV_VAR, None)

    def adapter(self, name="run"):
        adapter = DemoAdapter(self.root / name)
        return adapter, DemoProposer(adapter.spec.channels["proposer"])

    def run_stream(self, adapter, proposer, library_root, **kwargs):
        return schedule.run_domain(
            DOMAIN,
            adapter.tasks(),
            K,
            adapter=adapter,
            prop=proposer,
            runs_root=adapter.spec.runs_root,
            library_root=library_root,
            **kwargs,
        )

    def summary_path(self, adapter, task_id):
        return adapter.spec.runs_root / f"demo-oc-agg{K}" / task_id / "summary.json"

    def summary(self, adapter, task_id):
        return json.loads(self.summary_path(adapter, task_id).read_text())

    def libraries(self, adapter, library_root):
        surface = adapter.substrate.surface()
        return {
            mode: memory.connector_for(Path(library_root) / mode, DOMAIN, surface)
            for mode in schedule.MODES
        }

    def test_default_records_nothing(self):
        adapter, proposer = self.adapter()
        out = self.run_stream(adapter, proposer, self.root / "lib")
        self.assertEqual(out["failed"], 0)
        for task_id in adapter.tasks():
            self.assertNotIn("memory_recall", self.summary(adapter, task_id))

    def test_recall_is_injected_recorded_and_stripped_before_storing(self):
        adapter, proposer = self.adapter()
        surface = adapter.substrate.surface()
        lib_root = self.root / "lib"
        seen = []
        real = schedule._run_one_mode

        def spy(mode, task_id, *args, **kwargs):
            seen.append((mode, task_id, dict(kwargs["base_tree"])))
            return real(mode, task_id, *args, **kwargs)

        with patch.object(schedule, "_run_one_mode", side_effect=spy):
            out = self.run_stream(adapter, proposer, lib_root, memory_recall=True)
        self.assertEqual(out["failed"], 0)
        first, second = adapter.tasks()

        first_rec = self.summary(adapter, first)["memory_recall"]
        self.assertEqual(sorted(first_rec), sorted(schedule.MODES))
        for rec in first_rec.values():
            self.assertEqual(rec["text"], "")
            self.assertEqual(rec["file"], "LongTermMEMORY.md")

        rec = self.summary(adapter, second)["memory_recall"]
        self.assertEqual(sorted(rec), sorted(schedule.MODES))
        for mode in schedule.MODES:
            text = rec[mode]["text"]
            self.assertIn(LESSON, text)
            self.assertIn(f"## {library.slug(first)}", text)
            self.assertEqual(rec[mode]["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())

            trees = [t for m, tid, t in seen if m == mode and tid == second]
            self.assertEqual(len(trees), 1)
            body = trees[0]["LongTermMEMORY.md"]
            self.assertIn(recall.BEGIN + "\n" + text + "\n" + recall.END, body)
            self.assertLessEqual(
                len(harness_tree.always_on_text(surface, trees[0])), surface.always_on_max
            )

            sel = self.summary(adapter, second)["selections"][mode]
            adopted = schedule.Namespace("demo", "oc", mode, K).task_dir(
                adapter.spec.runs_root, second
            )
            adopted_tree = harness_tree.read_tree(
                surface, adopted / "harnesses" / sel["chosen_label"]
            )
            self.assertIn(recall.BEGIN, adopted_tree.get("LongTermMEMORY.md", ""))

        stored = [p for p in lib_root.rglob("*") if p.is_file() and "/harness/" in p.as_posix()]
        self.assertTrue(stored)
        for path in stored:
            body = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn(recall.BEGIN, body, path)
            self.assertNotIn(recall.END, body, path)

    def test_each_mode_recalls_only_its_own_library(self):
        adapter, proposer = self.adapter()
        lib_root = self.root / "shared"
        for mode in schedule.MODES:
            InMemoryConnector(lib_root / mode, DOMAIN).add(
                "foreign/task-9",
                {"AGENTS.md": "x"},
                experience={
                    "cue": f"{mode}-only cue",
                    "lesson": f"{mode}-only lesson",
                    "evidence": f"{mode}-only evidence",
                },
            )
            self.assertTrue(InMemoryConnector(lib_root / mode, DOMAIN).entries())
        with patch.dict(os.environ, {memory.ENV_VAR: CONNECTOR}):
            out = self.run_stream(adapter, proposer, lib_root, memory_scope="s", memory_recall=True)
        self.assertEqual(out["failed"], 0)
        for task_id in adapter.tasks():
            rec = self.summary(adapter, task_id)["memory_recall"]
            for mode in schedule.MODES:
                other = next(m for m in schedule.MODES if m != mode)
                self.assertIn(f"{mode}-only cue", rec[mode]["text"])
                self.assertNotIn(f"{other}-only", rec[mode]["text"])
        second = self.summary(adapter, adapter.tasks()[1])["memory_recall"]
        for mode in schedule.MODES:
            self.assertIn(LESSON, second[mode]["text"])

    def test_verdict_in_recall_fails_the_mode_without_injecting(self):
        memory.register(VERDICT, _VerdictConnector)
        self.addCleanup(memory._REGISTRY.pop, VERDICT, None)
        adapter, proposer = self.adapter()
        surface = adapter.substrate.surface()
        libs = {
            mode: memory.connector_for(self.root / "lib" / mode, DOMAIN, surface, name=VERDICT)
            for mode in schedule.MODES
        }
        with patch.object(schedule, "_run_one_mode") as run_one:
            res = schedule.run_task(
                adapter.tasks()[0],
                K,
                adapter=adapter,
                prop=proposer,
                runs_root=adapter.spec.runs_root,
                libraries=libs,
                memory_recall=True,
            )
        run_one.assert_not_called()
        for mode in schedule.MODES:
            self.assertFalse(res.modes[mode]["ok"])
            self.assertIn("ValueError", res.modes[mode]["error"])
        self.assertEqual(res.memory_recall, {})
        self.assertNotIn("memory_recall", self.summary(adapter, adapter.tasks()[0]))

    def test_rerun_reuses_the_recorded_text(self):
        adapter, proposer = self.adapter()
        lib_root = self.root / "lib"
        self.run_stream(adapter, proposer, lib_root, memory_recall=True)
        second = adapter.tasks()[1]
        recorded = self.summary(adapter, second)["memory_recall"]
        self.assertIn(LESSON, recorded["agg"]["text"])

        with patch.object(memory, "recall", wraps=memory.recall) as recall_fn:
            res = schedule.run_task(
                second,
                K,
                adapter=adapter,
                prop=proposer,
                runs_root=adapter.spec.runs_root,
                libraries=self.libraries(adapter, lib_root),
                memory_recall=True,
            )
        self.assertEqual(res.status, "skipped")
        self.assertEqual(recall_fn.call_count, 0)
        self.assertEqual(res.to_dict()["memory_recall"], recorded)

        path = self.summary_path(adapter, second)
        got = json.loads(path.read_text())
        got["status"] = "partial"
        path.write_text(json.dumps(got))
        seen = {}
        real = schedule._run_one_mode

        def spy(mode, task_id, *args, **kwargs):
            seen[mode] = kwargs["base_tree"]
            return real(mode, task_id, *args, **kwargs)

        with (
            patch.object(memory, "recall", wraps=memory.recall) as recall_fn,
            patch.object(schedule, "_run_one_mode", side_effect=spy),
        ):
            res = schedule.run_task(
                second,
                K,
                adapter=adapter,
                prop=proposer,
                runs_root=adapter.spec.runs_root,
                libraries=self.libraries(adapter, lib_root),
                memory_recall=True,
            )
        self.assertEqual(recall_fn.call_count, 0)
        self.assertEqual(res.status, "ok")
        self.assertEqual(self.summary(adapter, second)["memory_recall"], recorded)
        for mode in schedule.MODES:
            self.assertIn(recorded[mode]["text"], seen[mode]["LongTermMEMORY.md"])

    def test_tampered_record_fails_the_mode(self):
        adapter, proposer = self.adapter()
        lib_root = self.root / "lib"
        self.run_stream(adapter, proposer, lib_root, memory_recall=True)
        second = adapter.tasks()[1]
        path = self.summary_path(adapter, second)
        got = json.loads(path.read_text())
        got["status"] = "partial"
        got["memory_recall"]["agg"]["text"] += " tampered"
        path.write_text(json.dumps(got))
        res = schedule.run_task(
            second,
            K,
            adapter=adapter,
            prop=proposer,
            runs_root=adapter.spec.runs_root,
            libraries=self.libraries(adapter, lib_root),
            modes=("agg",),
            memory_recall=True,
        )
        self.assertFalse(res.modes["agg"]["ok"])
        self.assertIn("sha256", res.modes["agg"]["error"])


if __name__ == "__main__":
    unittest.main()
