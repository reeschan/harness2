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

"""Shared memory scopes through run_domain: isolation, retain, and the summary keys."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_connectors import InMemoryConnector

from harness2.core import library, memory, schedule
from harness2.demo import DemoAdapter, DemoProposer

CONNECTOR = "memory_connectors:InMemoryConnector"
DOMAIN = "accounting"
K = 1
EXPERIENCE = {"cue": "a cue", "lesson": "a lesson", "evidence": "some evidence"}
FOREIGN = "foreign/task-9"


class MemoryScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        InMemoryConnector.reset()
        self.addCleanup(InMemoryConnector.reset)

    def adapter(self, name):
        adapter = DemoAdapter(self.root / name)
        return adapter, DemoProposer(adapter.spec.channels["proposer"])

    def run_stream(self, adapter, proposer, library_root, *, memory_scope="", connector=None):
        env = {memory.ENV_VAR: connector} if connector else {}
        with patch.dict(os.environ, env):
            if not connector:
                os.environ.pop(memory.ENV_VAR, None)
            return schedule.run_domain(
                DOMAIN,
                adapter.tasks(),
                K,
                adapter=adapter,
                prop=proposer,
                runs_root=adapter.spec.runs_root,
                library_root=library_root,
                memory_scope=memory_scope,
            )

    def conn(self, library_root, mode):
        return InMemoryConnector(Path(library_root) / mode, DOMAIN)

    def summary(self, adapter, task_id):
        path = adapter.spec.runs_root / f"demo-oc-agg{K}" / task_id / "summary.json"
        return json.loads(path.read_text())

    def seed_foreign(self, library_root):
        for mode in schedule.MODES:
            self.conn(library_root, mode).add(FOREIGN, {"AGENTS.md": "x"}, experience=EXPERIENCE)

    def test_scopes_with_different_roots_do_not_share_entries(self):
        scope_a, scope_b = self.root / "scope-a", self.root / "scope-b"
        self.seed_foreign(scope_a)
        adapter_a, prop_a = self.adapter("run-a")
        result = self.run_stream(adapter_a, prop_a, scope_a, memory_scope="a", connector=CONNECTOR)
        self.assertEqual((result["done"], result["failed"]), (len(adapter_a.tasks()), 0))
        for mode in schedule.MODES:
            self.assertEqual(self.conn(scope_b, mode).entries(), [])
            self.assertIn(library.slug(FOREIGN), self.conn(scope_a, mode).entries())

        adapter_b, prop_b = self.adapter("run-b")
        self.run_stream(adapter_b, prop_b, scope_b, memory_scope="b", connector=CONNECTOR)
        for mode in schedule.MODES:
            got_b = self.conn(scope_b, mode).entries()
            self.assertTrue(got_b, mode)
            self.assertNotIn(library.slug(FOREIGN), got_b)
            self.assertIn(library.slug(FOREIGN), self.conn(scope_a, mode).entries())
            for entry in got_b:
                self.assertEqual(
                    InMemoryConnector.data[(str(scope_b / mode), DOMAIN)][entry]["run_id"],
                    self.summary(adapter_b, entry)["selections"][mode]["chosen_run_id"],
                )

    def test_agg_and_seq_entries_stay_in_their_mode(self):
        root = self.root / "lib"
        adapter, proposer = self.adapter("run")
        self.run_stream(adapter, proposer, root, memory_scope="s", connector=CONNECTOR)
        for mode in schedule.MODES:
            stored = InMemoryConnector.data[(str(root / mode), DOMAIN)]
            expected = sorted(
                library.slug(t)
                for t in adapter.tasks()
                if (self.summary(adapter, t)["selections"].get(mode) or {}).get("ok")
                and library.has_experience(
                    self.summary(adapter, t)["selections"][mode].get("experience")
                )
            )
            self.assertTrue(expected, mode)
            self.assertEqual(sorted(stored), expected)
            for entry, record in stored.items():
                sel = self.summary(adapter, record["task_id"])["selections"][mode]
                self.assertEqual(
                    (record["label"], record["run_id"]),
                    (sel["chosen_label"], sel["chosen_run_id"]),
                )
        self.assertEqual(
            {key for key in InMemoryConnector.data if key[0].startswith(str(root))},
            {(str(root / mode), DOMAIN) for mode in schedule.MODES},
        )

    def test_scoped_retain_keeps_entries_of_other_runs(self):
        root = self.root / "shared"
        self.seed_foreign(root)
        adapter, proposer = self.adapter("run")
        first = self.run_stream(adapter, proposer, root, memory_scope="s", connector=CONNECTOR)
        second = self.run_stream(adapter, proposer, root, memory_scope="s", connector=CONNECTOR)
        self.assertEqual(first["failed"], 0)
        self.assertEqual(
            [row["status"] for row in second["tasks"]], ["skipped"] * len(adapter.tasks())
        )
        for mode in schedule.MODES:
            entries = self.conn(root, mode).entries()
            self.assertIn(library.slug(FOREIGN), entries)
            for task_id in adapter.tasks():
                self.assertIn(library.slug(task_id), entries)
            self.assertEqual(second["tasks"][0]["library_entries_before"][mode], 1)

        unscoped = self.root / "unscoped"
        self.seed_foreign(unscoped)
        other, other_prop = self.adapter("run-unscoped")
        self.run_stream(other, other_prop, unscoped, connector=CONNECTOR)
        for mode in schedule.MODES:
            self.assertNotIn(library.slug(FOREIGN), self.conn(unscoped, mode).entries())

    def test_summary_records_memory_settings_only_when_not_default(self):
        cases = [
            ("default", None, "", None),
            ("connector", CONNECTOR, "", {"memory_connector": CONNECTOR, "memory_scope": ""}),
            ("scope", None, "team", {"memory_connector": "file", "memory_scope": "team"}),
            (
                "both",
                CONNECTOR,
                "team",
                {"memory_connector": CONNECTOR, "memory_scope": "team"},
            ),
        ]
        for name, connector, scope, expected in cases:
            with self.subTest(case=name):
                adapter, proposer = self.adapter(f"run-{name}")
                self.run_stream(
                    adapter,
                    proposer,
                    self.root / f"lib-{name}",
                    memory_scope=scope,
                    connector=connector,
                )
                for task_id in adapter.tasks():
                    summary = self.summary(adapter, task_id)
                    if expected is None:
                        self.assertNotIn("memory_connector", summary)
                        self.assertNotIn("memory_scope", summary)
                    else:
                        self.assertEqual(
                            {
                                "memory_connector": summary["memory_connector"],
                                "memory_scope": summary["memory_scope"],
                            },
                            expected,
                        )

    def test_resumed_task_keeps_memory_settings_on_its_result(self):
        adapter, proposer = self.adapter("run")
        root = self.root / "lib"
        self.run_stream(adapter, proposer, root, memory_scope="s", connector=CONNECTOR)
        with patch.dict(os.environ, {memory.ENV_VAR: CONNECTOR}):
            res = schedule.run_task(
                adapter.tasks()[0],
                K,
                adapter=adapter,
                prop=proposer,
                runs_root=adapter.spec.runs_root,
                libraries={m: self.conn(root, m) for m in schedule.MODES},
                memory_meta={"memory_connector": CONNECTOR, "memory_scope": "s"},
            )
        self.assertEqual(res.status, "skipped")
        self.assertEqual(res.to_dict()["memory_connector"], CONNECTOR)
        self.assertEqual(res.to_dict()["memory_scope"], "s")


if __name__ == "__main__":
    unittest.main()
