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

"""Test-only memory connectors, loadable as `memory_connectors:<Attr>` import paths."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Sequence

from harness2.core import library
from harness2.core import tree as harness_tree
from harness2.core.tree import SurfaceSpec

SOME_CONSTANT = "not a factory"


class InMemoryConnector:
    """A MemoryConnector that keeps entries in a class-level dict keyed by (str(root), domain).

    Every instance with the same (root, domain) sees the same entries; `reset()` clears all.
    """

    data: ClassVar[dict[tuple[str, str], dict[str, dict]]] = {}

    def __init__(self, root: Path | str, domain: str, surface: SurfaceSpec | None = None):
        self.root = Path(root)
        self.domain = domain
        self.surface = surface

    @classmethod
    def reset(cls) -> None:
        cls.data.clear()

    @property
    def key(self) -> tuple[str, str]:
        return (str(self.root), self.domain)

    def _entries(self) -> dict[str, dict]:
        return self.data.setdefault(self.key, {})

    def entries(self) -> list[str]:
        return sorted(self._entries())

    def retain(self, task_ids: Sequence[str]) -> None:
        keep = {library.slug(task_id) for task_id in task_ids}
        entries = self._entries()
        for entry in [e for e in entries if e not in keep]:
            del entries[entry]

    def add(
        self,
        task_id: str,
        harness: harness_tree.Tree,
        *,
        why: str = "",
        label: str = "",
        run_id: str = "",
        experience: dict | None = None,
    ) -> Path | None:
        if not library.has_experience(experience):
            return None
        entry = library.slug(task_id)
        self._entries()[entry] = {
            "task_id": task_id,
            "harness": dict(harness),
            "why": why,
            "label": label,
            "run_id": run_id,
            "experience": dict(experience or {}),
        }
        return self.root / library.domain_dir(self.domain) / "harnesses" / entry

    def context_dir(self) -> Path | None:
        return None

    def recall(self, task_id: str, limit_chars: int) -> str:
        entries = self._entries()
        items = [(name, entries[name]["experience"]) for name in sorted(entries)]
        return library.recall_summary(items, limit_chars)


def not_a_connector(**_kwargs) -> object:
    """A factory whose result is not a MemoryConnector."""
    return object()
