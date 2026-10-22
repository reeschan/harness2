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

"""The memory connector contract checks of harness2.testing.memory_contract."""

from __future__ import annotations

import unittest
from pathlib import Path

from harness2.core import memory
from harness2.core.tree import SurfaceSpec
from harness2.testing.memory_contract import MemoryConnectorContractTests, check_memory_connector

try:
    from memory_connectors import InMemoryConnector
except ImportError:  # unittest loads this file as `tests.test_memory_contract`
    from tests.memory_connectors import InMemoryConnector

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


class FileConnectorContractTest(MemoryConnectorContractTests, unittest.TestCase):
    """The bundled file connector against the memory connector contract."""

    def make_connector(self, root: Path, domain: str) -> memory.MemoryConnector:
        return memory.FileConnector(root, domain, SURFACE)


class InMemoryConnectorContractTest(MemoryConnectorContractTests, unittest.TestCase):
    """The test-only in-memory connector against the memory connector contract."""

    def setUp(self) -> None:
        InMemoryConnector.reset()
        self.addCleanup(InMemoryConnector.reset)

    def make_connector(self, root: Path, domain: str) -> InMemoryConnector:
        return InMemoryConnector(root, domain, SURFACE)


class _AlmostConnector:
    """A connector that has everything but `context_dir`."""

    domain = "test"

    def entries(self) -> list[str]:
        return []

    def retain(self, task_ids) -> None:
        return None

    def add(self, task_id, harness, **kwargs):
        return None


class CheckMemoryConnectorTest(unittest.TestCase):
    def test_missing_member_is_an_assertion_error_naming_it(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_memory_connector(_AlmostConnector())
        self.assertIn("context_dir", str(caught.exception))
