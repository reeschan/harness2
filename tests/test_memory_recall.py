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

"""The optional recall capability of memory connectors and recall-block stripping in store."""

import tempfile
import unittest
from pathlib import Path

from harness2.core import library, memory, recall
from harness2.core import tree as harness_tree
from harness2.core.tree import SurfaceSpec

SURFACE = SurfaceSpec(
    name="test",
    always_on=("AGENTS.md",),
    always_on_max=1000,
    always_on_cap_reason="test",
    skill_dir="skills",
    skill_name_rule="test",
    trigger_hint="test",
    autotrigger_file="AGENTS.md",
    trigger_line="test",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=None,
)

EXP_A = {"cue": "alpha cue", "lesson": "alpha lesson", "evidence": "alpha evidence"}
EXP_B = {"cue": "beta cue", "lesson": "beta lesson", "evidence": "beta evidence"}
TREE = {"AGENTS.md": "# Agent\nbe brief\n", "skills/x/SKILL.md": "---\nname: x\n---\n"}


class NoRecallConnector:
    """The same shape as test_memory.FakeConnector: no `recall` at all."""

    def __init__(self, root=None, domain="d", surface=None):
        self.root = root
        self.domain = domain
        self.surface = surface
        self.calls = []

    def entries(self):
        return []

    def retain(self, task_ids):
        return None

    def add(self, task_id, harness, **kwargs):
        self.calls.append((task_id, harness, kwargs))
        return Path("stored")

    def context_dir(self):
        return None


class LongRecallConnector(NoRecallConnector):
    def recall(self, task_id, limit_chars):
        return "x" * (limit_chars + 50)


class MemoryRecallTest(unittest.TestCase):
    def test_connector_without_recall_gives_empty(self):
        conn = NoRecallConnector()
        self.assertIsInstance(conn, memory.MemoryConnector)
        self.assertEqual(memory.recall(conn, "t", 500), "")

    def test_non_positive_limit_gives_empty(self):
        conn = LongRecallConnector()
        self.assertEqual(memory.recall(conn, "t", 0), "")
        self.assertEqual(memory.recall(conn, "t", -5), "")

    def test_result_is_cut_to_limit(self):
        self.assertEqual(memory.recall(LongRecallConnector(), "t", 7), "x" * 7)


class _RecallContract:
    """Shared checks (b) and (c); subclasses provide `make()`."""

    def make(self):
        raise NotImplementedError

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.conn = self.make()

    def store_two(self):
        memory.store(self.conn, "task/a", TREE, why="w", label="l", experience=EXP_A)
        memory.store(self.conn, "task/b", TREE, why="w", label="l", experience=EXP_B)

    def test_empty_gives_empty(self):
        self.assertEqual(self.conn.recall("next", 1000), "")
        self.assertEqual(memory.recall(self.conn, "next", 1000), "")

    def test_contains_both_entries(self):
        self.store_two()
        text = memory.recall(self.conn, "next", 1000)
        for exp in (EXP_A, EXP_B):
            for value in exp.values():
                self.assertIn(value, text)
        self.assertIn("## " + library.slug("task/a"), text)
        self.assertIn("- cue: alpha cue", text)
        self.assertEqual(self.conn.recall("next", 1000), text)

    def test_retained_out_entry_is_excluded(self):
        self.store_two()
        self.conn.retain(["task/b"])
        text = self.conn.recall("next", 1000)
        self.assertIn("beta lesson", text)
        for value in EXP_A.values():
            self.assertNotIn(value, text)

    def test_length_within_limit(self):
        self.store_two()
        full = self.conn.recall("next", 10_000)
        for limit in (1, 5, 17, 40, len(full) - 1, len(full), len(full) + 10):
            with self.subTest(limit=limit):
                self.assertLessEqual(len(self.conn.recall("next", limit)), limit)
                self.assertLessEqual(len(memory.recall(self.conn, "next", limit)), limit)
        self.assertEqual(self.conn.recall("next", 5), full[:5])
        self.assertEqual(self.conn.recall("next", 0), "")


class LibraryRecallTest(_RecallContract, unittest.TestCase):
    def make(self):
        return library.Library(self.root / "lib", "dom", SURFACE)


class InMemoryRecallTest(_RecallContract, unittest.TestCase):
    def make(self):
        from memory_connectors import InMemoryConnector

        InMemoryConnector.reset()
        self.addCleanup(InMemoryConnector.reset)
        return memory.connector_for(
            self.root, "dom", SURFACE, name="memory_connectors:InMemoryConnector"
        )


class StoreStripsBlocksTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def injected(self):
        tree = recall.inject(SURFACE, TREE, "## old\n- cue: remembered")
        self.assertIn(recall.BEGIN, tree["AGENTS.md"])
        return tree

    def assert_no_block(self, tree: harness_tree.Tree):
        for rel, body in tree.items():
            self.assertNotIn(recall.BEGIN, body, rel)
            self.assertNotIn(recall.END, body, rel)
            self.assertNotIn("remembered", body, rel)

    def test_library_files_have_no_block(self):
        lib = library.Library(self.root / "lib", "dom", SURFACE)
        memory.store(lib, "task/a", self.injected(), experience=EXP_A)
        stored = lib.tree_of("task/a")
        self.assert_no_block(stored)
        self.assertEqual(stored, TREE)
        for path in lib.entry_dir("task/a").rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(recall.BEGIN, text, path)
                self.assertNotIn(recall.END, text, path)

    def test_in_memory_entry_has_no_block(self):
        from memory_connectors import InMemoryConnector

        InMemoryConnector.reset()
        self.addCleanup(InMemoryConnector.reset)
        conn = InMemoryConnector(self.root, "dom", SURFACE)
        memory.store(conn, "task/a", self.injected(), experience=EXP_A)
        harness = conn.data[conn.key][library.slug("task/a")]["harness"]
        self.assert_no_block(harness)
        self.assertEqual(harness, TREE)

    def test_block_only_file_is_dropped(self):
        conn = NoRecallConnector()
        tree = recall.inject(SURFACE, {"skills/x/SKILL.md": "s\n"}, "note")
        memory.store(conn, "task/a", tree, experience=EXP_A)
        self.assertEqual(conn.calls[0][1], {"skills/x/SKILL.md": "s\n"})


if __name__ == "__main__":
    unittest.main()
