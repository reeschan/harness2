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


class _CountingCloseConnector(InMemoryConnector):
    """An in-memory connector that counts how often `close()` is called."""

    def __init__(self, root: Path, domain: str, surface: SurfaceSpec | None = None) -> None:
        super().__init__(root, domain, surface)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class CloseConnectorContractTest(MemoryConnectorContractTests, unittest.TestCase):
    """An in-memory connector with a `close()` against the memory connector contract."""

    def setUp(self) -> None:
        InMemoryConnector.reset()
        self.addCleanup(InMemoryConnector.reset)
        self.connectors: list[_CountingCloseConnector] = []
        self.addCleanup(self._assert_connectors_closed)

    def make_connector(self, root: Path, domain: str) -> _CountingCloseConnector:
        conn = _CountingCloseConnector(root, domain, SURFACE)
        self.connectors.append(conn)
        return conn

    def _assert_connectors_closed(self) -> None:
        for conn in self.connectors:
            if conn.close_calls < 1:
                raise AssertionError("the contract tests must close the connector they built")


class _AlmostConnector:
    """A connector that has everything but `context_dir`."""

    domain = "test"

    def entries(self) -> list[str]:
        return []

    def retain(self, task_ids) -> None:
        return None

    def add(self, task_id, harness, **kwargs):
        return None


class _BareConnector:
    """A connector with the required members but neither `recall` nor `close`."""

    domain = "test"

    def entries(self) -> list[str]:
        return []

    def retain(self, task_ids) -> None:
        return None

    def add(self, task_id, harness, **kwargs):
        return None

    def context_dir(self) -> Path | None:
        return None


class CheckMemoryConnectorTest(unittest.TestCase):
    def test_missing_member_is_an_assertion_error_naming_it(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_memory_connector(_AlmostConnector())
        self.assertIn("context_dir", str(caught.exception))

    def test_connector_without_recall_or_close_passes(self) -> None:
        self.assertIsNone(check_memory_connector(_BareConnector()))

    def _rejected(self, member: str, value: object) -> None:
        conn = _BareConnector()
        setattr(conn, member, value)
        with self.assertRaises(AssertionError) as caught:
            check_memory_connector(conn)
        self.assertIn(f"{member} (callable)", str(caught.exception))

    def test_recall_none_is_an_assertion_error_naming_it(self) -> None:
        self._rejected("recall", None)

    def test_recall_not_callable_is_an_assertion_error_naming_it(self) -> None:
        self._rejected("recall", 5)

    def test_close_none_is_an_assertion_error_naming_it(self) -> None:
        self._rejected("close", None)

    def test_close_not_callable_is_an_assertion_error_naming_it(self) -> None:
        self._rejected("close", "x")
