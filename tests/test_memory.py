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

"""Connector registry and store checks for core.memory."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2.core import context, library, memory, schedule
from harness2.core.tree import SurfaceSpec
from harness2.demo import DemoAdapter, DemoProposer

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

EXPERIENCE = {"cue": "a cue", "lesson": "a lesson", "evidence": "some evidence"}
MARKER = context.GRADER_VERDICT_MARKERS[0]


class FakeConnector:
    """Records every `add` call and returns a fixed path."""

    def __init__(self, root=None, domain="d", surface=None):
        self.root = root
        self.domain = domain
        self.surface = surface
        self.calls = []
        self.result = Path("stored")

    def entries(self):
        return []

    def retain(self, task_ids):
        return None

    def add(self, task_id, harness, **kwargs):
        self.calls.append((task_id, harness, kwargs))
        return self.result

    def context_dir(self):
        return None


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def register(self, name, factory):
        memory.register(name, factory)
        self.addCleanup(memory._REGISTRY.pop, name, None)

    def test_default_is_the_file_library(self):
        conn = memory.connector_for(self.root, "dom", SURFACE, env={})
        self.assertIsInstance(conn, library.Library)
        self.assertIsInstance(conn, memory.MemoryConnector)
        self.assertIs(memory.FileConnector, library.Library)
        self.assertIn("file", memory.names())
        self.assertEqual(conn.root, self.root)
        self.assertEqual(conn.domain, "dom")
        self.assertIs(conn.surface, SURFACE)

    def test_unknown_name_raises_value_error(self):
        with self.assertRaises(ValueError) as caught:
            memory.connector_for(self.root, "dom", SURFACE, env={memory.ENV_VAR: "nope"})
        self.assertIn("file", str(caught.exception))
        with self.assertRaises(ValueError):
            memory.connector_for(self.root, "dom", SURFACE, name="nope", env={})

    def test_factory_returning_non_connector_raises_type_error(self):
        self.register("bad", lambda **kwargs: object())
        with self.assertRaises(TypeError):
            memory.connector_for(self.root, "dom", SURFACE, name="bad", env={})

    def test_registered_name_selected_by_name_or_env_and_name_wins(self):
        self.register("fake", FakeConnector)
        by_name = memory.connector_for(self.root, "dom", SURFACE, name="fake", env={})
        self.assertIsInstance(by_name, FakeConnector)
        self.assertEqual(by_name.root, self.root)
        self.assertEqual(by_name.domain, "dom")
        self.assertIs(by_name.surface, SURFACE)
        by_env = memory.connector_for(self.root, "dom", SURFACE, env={memory.ENV_VAR: "fake"})
        self.assertIsInstance(by_env, FakeConnector)
        both = memory.connector_for(
            self.root, "dom", SURFACE, name="file", env={memory.ENV_VAR: "fake"}
        )
        self.assertIsInstance(both, library.Library)
        self.assertIn("fake", memory.names())


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConnector()

    def test_incomplete_experience_is_not_stored(self):
        cases = [None] + [{**EXPERIENCE, key: ""} for key in ("cue", "lesson", "evidence")]
        for experience in cases:
            with self.subTest(experience=experience):
                result = memory.store(
                    self.conn, "t1", {"AGENTS.md": "ok"}, why="w", experience=experience
                )
                self.assertIsNone(result)
        self.assertEqual(self.conn.calls, [])

    def test_marker_in_tree_body_raises(self):
        with self.assertRaises(ValueError):
            memory.store(
                self.conn, "t1", {"notes.md": f"x {MARKER} y"}, experience=dict(EXPERIENCE)
            )
        self.assertEqual(self.conn.calls, [])

    def test_marker_in_why_raises(self):
        with self.assertRaises(ValueError):
            memory.store(
                self.conn,
                "t1",
                {"AGENTS.md": "ok"},
                why=f"because {MARKER}",
                experience=dict(EXPERIENCE),
            )
        self.assertEqual(self.conn.calls, [])

    def test_valid_store_calls_add_once_with_arguments(self):
        harness = {"AGENTS.md": "ok"}
        experience = dict(EXPERIENCE)
        result = memory.store(
            self.conn,
            "t1",
            harness,
            why="w",
            label="L",
            run_id="r1",
            experience=experience,
        )
        self.assertIs(result, self.conn.result)
        self.assertEqual(
            self.conn.calls,
            [
                (
                    "t1",
                    harness,
                    {"why": "w", "label": "L", "run_id": "r1", "experience": experience},
                )
            ],
        )


class LenLibrary(library.Library):
    """A file library that is falsy while empty, so truthiness checks on it would misfire."""

    def __len__(self):
        return len(self.entries())


class RunDomainConnectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adapter = DemoAdapter(self.root)
        self.proposer = DemoProposer(self.adapter.spec.channels["proposer"])

    def test_env_selected_connector_is_used_and_written(self):
        made = []

        def factory(**kwargs):
            conn = LenLibrary(**kwargs)
            made.append(conn)
            return conn

        memory.register("lenlib", factory)
        self.addCleanup(memory._REGISTRY.pop, "lenlib", None)
        tasks = self.adapter.tasks()
        with patch.dict(os.environ, {memory.ENV_VAR: "lenlib"}):
            result = schedule.run_domain(
                "accounting",
                tasks,
                1,
                adapter=self.adapter,
                prop=self.proposer,
                runs_root=self.adapter.spec.runs_root,
                library_root=self.root / "library",
            )
        self.assertGreaterEqual(len(made), len(schedule.MODES))
        self.assertGreaterEqual(len(result["tasks"]), 2)
        before = result["tasks"][1]["library_entries_before"]
        self.assertTrue(any(n >= 1 for n in before.values()), before)


if __name__ == "__main__":
    unittest.main()
