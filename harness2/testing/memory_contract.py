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

"""Contract checks for memory connectors, for connector authors to run from their own tests.

The memory connector contract is described in `harness2.core.memory`: the five members of
`MemoryConnector` (`domain`, `entries()`, `retain()`, `add()` and `context_dir()`), plus two
optional capabilities read through `memory`: `recall(task_id, limit_chars)` and `close()`.
`memory.store` runs the grader-verdict checks before handing an experience to any connector, so
those checks hold whatever the backend does.

- `check_memory_connector(conn)` raises AssertionError when `conn` is not a MemoryConnector:
  `domain` must be a str, `entries` / `retain` / `add` / `context_dir` must be callable, and the
  optional `recall` and `close` attributes, when present (even as None), must be callable too.
  The message names the missing or invalid members.
- `MemoryConnectorContractTests` is a unittest mixin that runs these checks, and a few more,
  against the connector your test builds: `check_memory_connector`; a new connector has no
  entries and its `context_dir()` is None or an existing directory; storing two complete
  experiences gives their `library.slug` names in ascending order; `retain()` drops the other
  entries; storing the same task_id twice keeps one entry; `recall()` (if any) is empty before
  anything is stored, returns str afterwards, and is cut to the requested limit; an incomplete
  experience adds no entry. Each test closes its connector with `memory.close` afterwards.
  `close()` is optional: a connector without it meets the contract, and
  `test_memory_contract_close_is_optional_and_callable` passes too. A `close` that is present
  must be callable; `check_memory_connector` rejects one set to None.

Example::

    import unittest
    from pathlib import Path

    from harness2.testing.memory_contract import MemoryConnectorContractTests
    from my_package.memory import MyConnector


    class MyConnectorContractTest(MemoryConnectorContractTests, unittest.TestCase):
        def make_connector(self, root: Path, domain: str):
            return MyConnector(root=root, domain=domain)

The mixin does not inherit from `unittest.TestCase`, so unittest does not collect it on its
own; list `unittest.TestCase` after it in your test class. Each test calls `make_connector`
with a fresh temporary directory and the domain `contract`. The mixin passes no settings of its
own: a subclass whose connector needs more writes the values inside `make_connector`, for
example `return MyConnector(root=root, domain=domain, timeout=30)`, because harness2's
`--memory-opt` values do not reach a contract test.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from harness2.core import library, memory
from harness2.core.tree import SurfaceSpec

_DOMAIN = "contract"

_MISSING = object()

_SURFACE = SurfaceSpec(
    name="contract",
    always_on=("AGENTS.md",),
    always_on_max=1000,
    always_on_cap_reason="contract checks",
    skill_dir="skills",
    skill_name_rule="test",
    trigger_hint="test",
    autotrigger_file="AGENTS.md",
    trigger_line="test",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=None,
)

_TREE = {
    "AGENTS.md": "# Agent\nbe brief\n",
    "skills/x/SKILL.md": "---\nname: x\n---\n",
}

_EXPERIENCE_A = {"cue": "alpha cue", "lesson": "alpha lesson", "evidence": "alpha evidence"}
_EXPERIENCE_B = {"cue": "beta cue", "lesson": "beta lesson", "evidence": "beta evidence"}


def check_memory_connector(conn: Any) -> None:
    """Assert that `conn` meets the memory connector contract.

    `domain` must be a str; `entries`, `retain`, `add` and `context_dir` must be callable. The
    optional `recall` and `close` attributes, when present (even as None), must be callable. A
    missing or invalid member raises AssertionError whose message names it.
    """
    problems: list[str] = []
    if not isinstance(getattr(conn, "domain", None), str):
        problems.append("domain (str)")
    for name in ("entries", "retain", "add", "context_dir"):
        if not callable(getattr(conn, name, None)):
            problems.append(name)
    recall = getattr(conn, "recall", _MISSING)
    if recall is not _MISSING and not callable(recall):
        problems.append("recall (callable)")
    close = getattr(conn, "close", _MISSING)
    if close is not _MISSING and not callable(close):
        problems.append("close (callable)")
    if problems or not isinstance(conn, memory.MemoryConnector):
        detail = ", ".join(problems) or "the MemoryConnector Protocol check failed"
        raise AssertionError(
            f"{type(conn).__name__} is not a MemoryConnector; missing or invalid: {detail}"
        )


class MemoryConnectorContractTests:
    """A unittest mixin that checks a connector against the memory connector contract.

    Subclass it together with `unittest.TestCase` and implement `make_connector(root, domain)`.
    Each test passes a fresh, empty temporary directory as `root` and `"contract"` as `domain`,
    and closes the connector it built with `memory.close` afterwards.
    """

    def make_connector(self, root: Path, domain: str) -> memory.MemoryConnector:
        """Build the connector under test for `root` and `domain`."""
        raise NotImplementedError(
            "subclasses of MemoryConnectorContractTests implement make_connector"
        )

    # -- helpers -------------------------------------------------------------------------

    def _tmp(self) -> Path:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)  # type: ignore[attr-defined]
        return Path(holder.name)

    def _connector(self, tmp: Path) -> memory.MemoryConnector:
        root = tmp / "memory"
        root.mkdir()
        return self.make_connector(root, _DOMAIN)

    def _store(self, conn: memory.MemoryConnector, task_id: str, experience: dict) -> None:
        memory.store(conn, task_id, _TREE, why="contract", label="contract", experience=experience)

    # -- tests ---------------------------------------------------------------------------

    def test_memory_contract_check_memory_connector(self) -> None:
        conn = self._connector(self._tmp())
        self.addCleanup(memory.close, conn)
        check_memory_connector(conn)

    def test_memory_contract_empty(self) -> None:
        conn = self._connector(self._tmp())
        self.addCleanup(memory.close, conn)
        entries = conn.entries()
        if entries != []:
            raise AssertionError(f"entries() must be [] for a new connector, got {entries!r}")
        context_dir = conn.context_dir()
        if context_dir is not None and not Path(context_dir).is_dir():
            raise AssertionError(
                f"context_dir() must be None or an existing directory, got {context_dir!r}"
            )

    def test_memory_contract_stored_entries_are_sorted(self) -> None:
        conn = self._connector(self._tmp())
        self.addCleanup(memory.close, conn)
        self._store(conn, "t1", _EXPERIENCE_A)
        self._store(conn, "t2", _EXPERIENCE_B)
        expected = sorted([library.slug("t1"), library.slug("t2")])
        if conn.entries() != expected:
            raise AssertionError(
                f"entries() must be {expected!r} after storing t1 and t2, got {conn.entries()!r}"
            )

    def test_memory_contract_retain_keeps_only_listed(self) -> None:
        conn = self._connector(self._tmp())
        self.addCleanup(memory.close, conn)
        self._store(conn, "t1", _EXPERIENCE_A)
        self._store(conn, "t2", _EXPERIENCE_B)
        conn.retain(["t1"])
        expected = [library.slug("t1")]
        if conn.entries() != expected:
            raise AssertionError(
                f"entries() must be {expected!r} after retain(['t1']), got {conn.entries()!r}"
            )

    def test_memory_contract_same_task_id_stores_one_entry(self) -> None:
        conn = self._connector(self._tmp())
        self.addCleanup(memory.close, conn)
        self._store(conn, "t1", _EXPERIENCE_A)
        self._store(conn, "t1", _EXPERIENCE_B)
        expected = [library.slug("t1")]
        if conn.entries() != expected:
            raise AssertionError(
                f"storing t1 twice must leave one entry {expected!r}, got {conn.entries()!r}"
            )

    def test_memory_contract_recall(self) -> None:
        conn = self._connector(self._tmp())
        self.addCleanup(memory.close, conn)
        if not callable(getattr(conn, "recall", None)):
            return
        empty = memory.recall(conn, "t3", 10_000)
        if empty != "":
            raise AssertionError(f"recall() must be '' for a new connector, got {empty!r}")
        self._store(conn, "t1", _EXPERIENCE_A)
        self._store(conn, "t2", _EXPERIENCE_B)
        full = memory.recall(conn, "t3", 10_000)
        if not isinstance(full, str):
            raise AssertionError(f"recall() must return str, got {type(full).__name__}")
        short = memory.recall(conn, "t3", 5)
        if len(short) > 5:
            raise AssertionError(f"recall(limit_chars=5) returned {len(short)} characters")

    def test_memory_contract_incomplete_experience_is_not_stored(self) -> None:
        conn = self._connector(self._tmp())
        self.addCleanup(memory.close, conn)
        result = memory.store(conn, "t1", _TREE, experience={"cue": "a cue", "lesson": "a lesson"})
        if result is not None:
            raise AssertionError(f"an incomplete experience must store nothing, got {result!r}")
        if conn.entries() != []:
            raise AssertionError(
                f"an incomplete experience must add no entry, got {conn.entries()!r}"
            )

    def test_memory_contract_close_is_optional_and_callable(self) -> None:
        conn = self._connector(self._tmp())
        memory.close(conn)
