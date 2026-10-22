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

"""Entry point lookup in core.memory: resolve_factory, connector_for and names."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from memory_connectors import InMemoryConnector

from harness2.core import library, memory
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

IMPORT_PATH = "memory_connectors:InMemoryConnector"


def entry_factory(root, domain, surface=None):
    """A fake entry point target: builds an in-memory connector."""
    return InMemoryConnector(root, domain, surface)


class FakeEntryPoint:
    def __init__(self, name, target):
        self.name = name
        self._target = target
        self.loaded = 0

    def load(self):
        self.loaded += 1
        return self._target


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        InMemoryConnector.reset()
        self.addCleanup(InMemoryConnector.reset)

    def patch_entry_points(self, *eps):
        patcher = patch.object(memory, "_entry_points", lambda: list(eps))
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_built(self, conn):
        self.assertIsInstance(conn, InMemoryConnector)
        self.assertIsInstance(conn, memory.MemoryConnector)
        self.assertEqual(conn.root, self.root)
        self.assertEqual(conn.domain, "dom")
        self.assertIs(conn.surface, SURFACE)

    def test_group_name(self):
        self.assertEqual(memory.ENTRY_POINT_GROUP, "harness2.memory_connectors")

    def test_entry_points_asks_for_the_group(self):
        calls = []
        marker = object()

        def entry_points(**kwargs):
            calls.append(kwargs)
            return [marker]

        with mock.patch("importlib.metadata.entry_points", entry_points):
            self.assertEqual(memory._entry_points(), [marker])
        self.assertEqual(calls, [{"group": memory.ENTRY_POINT_GROUP}])

    def test_resolve_by_entry_point_name(self):
        self.patch_entry_points(FakeEntryPoint("ext", InMemoryConnector))
        self.assertIs(memory.resolve_factory("ext"), InMemoryConnector)
        self.assert_built(memory.connector_for(self.root, "dom", SURFACE, name="ext", env={}))

    def test_entry_point_by_name_and_by_env(self):
        self.patch_entry_points(FakeEntryPoint("ext", entry_factory))
        self.assert_built(memory.connector_for(self.root, "dom", SURFACE, name="ext", env={}))
        self.assert_built(
            memory.connector_for(self.root, "dom", SURFACE, env={memory.ENV_VAR: "ext"})
        )
        with patch.dict(os.environ, {memory.ENV_VAR: "ext"}):
            self.assert_built(memory.connector_for(self.root, "dom", SURFACE))

    def test_builtin_registration_comes_first(self):
        ep = FakeEntryPoint("file", InMemoryConnector)
        self.patch_entry_points(ep)
        self.assertIs(memory.resolve_factory("file"), library.Library)
        conn = memory.connector_for(self.root, "dom", SURFACE, name="file", env={})
        self.assertIsInstance(conn, library.Library)
        self.assertNotIsInstance(conn, InMemoryConnector)
        self.assertEqual(ep.loaded, 0)
        self.assertEqual(memory.names().count("file"), 1)

    def test_entry_point_comes_before_import_path(self):
        self.patch_entry_points(FakeEntryPoint(IMPORT_PATH, entry_factory))
        self.assertIs(memory.resolve_factory(IMPORT_PATH), entry_factory)

    def test_import_path_still_works(self):
        self.patch_entry_points()
        self.assertIs(memory.resolve_factory(IMPORT_PATH), InMemoryConnector)
        self.assert_built(memory.connector_for(self.root, "dom", SURFACE, name=IMPORT_PATH, env={}))

    def test_load_error_raises_value_error_with_name_and_group(self):
        class Broken:
            name = "broken"

            def load(self):
                raise ImportError("no module")

        self.patch_entry_points(Broken())
        with self.assertRaises(ValueError) as caught:
            memory.connector_for(self.root, "dom", SURFACE, name="broken", env={})
        message = str(caught.exception)
        self.assertIn("broken", message)
        self.assertIn(memory.ENTRY_POINT_GROUP, message)

    def test_uncallable_entry_point_raises_type_error(self):
        self.patch_entry_points(FakeEntryPoint("number", 42))
        with self.assertRaises(TypeError) as caught:
            memory.connector_for(self.root, "dom", SURFACE, name="number", env={})
        self.assertIn("number", str(caught.exception))

    def test_unknown_name_lists_registered_and_entry_points(self):
        self.patch_entry_points(FakeEntryPoint("ext", entry_factory))
        with self.assertRaises(ValueError) as caught:
            memory.connector_for(self.root, "dom", SURFACE, name="nope", env={})
        message = str(caught.exception)
        self.assertIn("unknown memory connector 'nope'", message)
        self.assertIn("file", message)
        self.assertIn("ext", message)
        self.assertIn(memory.ENTRY_POINT_GROUP, message)

    def test_names_include_entry_points(self):
        self.patch_entry_points(
            FakeEntryPoint("b-ext", entry_factory),
            FakeEntryPoint("a-ext", entry_factory),
            FakeEntryPoint("file", InMemoryConnector),
        )
        names = memory.names()
        self.assertIn("a-ext", names)
        self.assertIn("b-ext", names)
        self.assertIn("file", names)
        self.assertEqual(names, sorted(set(names)))


if __name__ == "__main__":
    unittest.main()
