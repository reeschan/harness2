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

"""Memory connectors: the file default is the published library, others plug in by name."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2.core import memory, schedule
from harness2.core.library import Library
from harness2.demo import DemoAdapter, DemoProposer


class RecordingConnector(Library):
    """A file library that also records which roots were opened through the registry."""

    opened: list = []

    def __init__(self, *, root, domain, surface):
        super().__init__(root=root, domain=domain, surface=surface)
        RecordingConnector.opened.append((Path(root).name, domain))


class MemoryConnectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adapter = DemoAdapter(self.root)
        self.surface = self.adapter.substrate.surface()
        self.addCleanup(memory._FACTORIES.pop, "recording", None)

    def test_file_connector_is_the_default_library(self):
        conn = memory.connector_for(self.root / "agg", "accounting", self.surface, env={})
        self.assertIsInstance(conn, Library)
        self.assertIsInstance(conn, memory.MemoryConnector)
        self.assertEqual(conn.dir, self.root / "agg" / "accounting")
        self.assertIsNone(conn.context_dir())
        self.assertEqual(conn.entries(), [])

    def test_unknown_or_invalid_connector_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown memory connector 'nope'"):
            memory.connector_for(self.root, "d", self.surface, env={memory.ENV_VAR: "nope"})
        memory.register("recording", lambda **kw: object())
        with self.assertRaises(TypeError):
            memory.connector_for(self.root, "d", self.surface, name="recording")
        with self.assertRaises(ValueError):
            memory.register(" padded", Library)

    def test_run_domain_uses_the_connector_named_in_the_environment(self):
        RecordingConnector.opened = []
        memory.register("recording", RecordingConnector)
        with patch.dict("os.environ", {memory.ENV_VAR: "recording"}):
            result = schedule.run_domain(
                "accounting",
                self.adapter.tasks(),
                1,
                adapter=self.adapter,
                prop=DemoProposer(self.adapter.spec.channels["proposer"]),
                runs_root=self.adapter.spec.runs_root,
                library_root=self.root / "library",
            )
        self.assertEqual(result["failed"], 0)
        self.assertEqual(
            sorted(RecordingConnector.opened), [("agg", "accounting"), ("seq", "accounting")]
        )


if __name__ == "__main__":
    unittest.main()
