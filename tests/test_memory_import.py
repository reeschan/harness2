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

"""`module:Attr` import paths in core.memory.connector_for, and store with an in-memory
connector."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_connectors import InMemoryConnector

from harness2.core import context, library, memory
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

EXPERIENCE = {"cue": "a cue", "lesson": "a lesson", "evidence": "some evidence"}
IMPORT_PATH = "memory_connectors:InMemoryConnector"


class Recorder:
    """A connector that remembers it was built by a registered factory."""

    def __init__(self, root=None, domain="d", surface=None):
        self.root = root
        self.domain = domain
        self.surface = surface

    def entries(self):
        return []

    def retain(self, task_ids):
        return None

    def add(self, task_id, harness, **kwargs):
        return None

    def context_dir(self):
        return None


class ImportPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        InMemoryConnector.reset()
        self.addCleanup(InMemoryConnector.reset)

    def register(self, name, factory):
        memory.register(name, factory)
        self.addCleanup(memory._REGISTRY.pop, name, None)

    def assert_built(self, conn):
        self.assertIsInstance(conn, InMemoryConnector)
        self.assertIsInstance(conn, memory.MemoryConnector)
        self.assertEqual(conn.root, self.root)
        self.assertEqual(conn.domain, "dom")
        self.assertIs(conn.surface, SURFACE)

    def test_import_path_by_name_and_by_env(self):
        self.assert_built(memory.connector_for(self.root, "dom", SURFACE, name=IMPORT_PATH, env={}))
        self.assert_built(
            memory.connector_for(self.root, "dom", SURFACE, env={memory.ENV_VAR: IMPORT_PATH})
        )
        with patch.dict(os.environ, {memory.ENV_VAR: IMPORT_PATH}):
            self.assert_built(memory.connector_for(self.root, "dom", SURFACE))

    def test_registered_names_win_over_import_paths(self):
        conn = memory.connector_for(
            self.root, "dom", SURFACE, name="file", env={memory.ENV_VAR: IMPORT_PATH}
        )
        self.assertIsInstance(conn, library.Library)
        self.register(IMPORT_PATH, Recorder)
        conn = memory.connector_for(self.root, "dom", SURFACE, name=IMPORT_PATH, env={})
        self.assertIsInstance(conn, Recorder)
        self.assertNotIsInstance(conn, InMemoryConnector)

    def test_malformed_missing_and_unimportable_names_raise_value_error(self):
        names = [
            "a:b:c",
            ":X",
            "mod:",
            "harness2_no_such_module_xyz:Factory",
            "memory_connectors:NoSuchAttr",
            "memory_connectors:InMemoryConnector.no_such_attr",
            "no_colon_and_unregistered",
        ]
        for name in names:
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    memory.connector_for(self.root, "dom", SURFACE, name=name, env={})
                self.assertIn(name, str(caught.exception))

    def test_uncallable_attribute_and_non_connector_result_raise_type_error(self):
        for name in ("memory_connectors:SOME_CONSTANT", "memory_connectors:not_a_connector"):
            with self.subTest(name=name):
                with self.assertRaises(TypeError) as caught:
                    memory.connector_for(self.root, "dom", SURFACE, name=name, env={})
                self.assertIn(name, str(caught.exception))

    def test_connector_name_precedence(self):
        env = {memory.ENV_VAR: "from-env"}
        self.assertEqual(memory.connector_name(name="explicit", env=env), "explicit")
        self.assertEqual(memory.connector_name(env=env), "from-env")
        self.assertEqual(memory.connector_name(env={}), "file")
        self.assertEqual(memory.connector_name(env={memory.ENV_VAR: ""}), "file")
        with patch.dict(os.environ, {memory.ENV_VAR: "os-env"}):
            self.assertEqual(memory.connector_name(), "os-env")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(memory.connector_name(), memory.DEFAULT)


class InMemoryStoreTests(unittest.TestCase):
    def setUp(self):
        InMemoryConnector.reset()
        self.addCleanup(InMemoryConnector.reset)
        self.conn = memory.connector_for("mem-root", "dom", SURFACE, name=IMPORT_PATH, env={})

    def test_grader_verdict_markers_are_rejected(self):
        for marker in context.GRADER_VERDICT_MARKERS:
            cases = [
                ({"notes.md": f"x {marker} y"}, ""),
                ({"AGENTS.md": "ok"}, f"because {marker}"),
            ]
            for harness, why in cases:
                with self.subTest(marker=marker, harness=harness, why=why):
                    with self.assertRaises(ValueError):
                        memory.store(
                            self.conn, "t/1", harness, why=why, experience=dict(EXPERIENCE)
                        )
                    self.assertEqual(self.conn.entries(), [])

    def test_valid_experience_adds_one_entry(self):
        result = memory.store(
            self.conn, "t/1", {"AGENTS.md": "ok"}, why="w", experience=dict(EXPERIENCE)
        )
        self.assertIsNotNone(result)
        self.assertEqual(self.conn.entries(), [library.slug("t/1")])
        same_key = InMemoryConnector("mem-root", "dom", SURFACE)
        self.assertEqual(same_key.entries(), [library.slug("t/1")])
        self.assertEqual(InMemoryConnector("mem-root", "other", SURFACE).entries(), [])

    def test_incomplete_experience_and_retain(self):
        self.assertIsNone(memory.store(self.conn, "t1", {"AGENTS.md": "ok"}, experience=None))
        self.assertEqual(self.conn.entries(), [])
        for task_id in ("a/1", "b/2", "c/3"):
            memory.store(self.conn, task_id, {"AGENTS.md": "ok"}, experience=dict(EXPERIENCE))
        self.conn.retain(["a/1", "c/3"])
        self.assertEqual(self.conn.entries(), ["a__1", "c__3"])
        self.assertIsNone(self.conn.context_dir())


if __name__ == "__main__":
    unittest.main()
