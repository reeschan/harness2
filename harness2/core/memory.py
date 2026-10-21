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

"""Memory connectors: where the per-domain experience library R lives.

The schedule only needs four operations from a library, so any store that offers them can
stand in for the on-disk one:

    entries()                 task slugs already stored for this domain
    retain(task_ids)          keep only entries for these (earlier) tasks
    add(task_id, tree, ...)   store one adopted harness and its experience
    context_dir()             a directory to hand an improver session, or None when empty

`FileConnector` (the `library.Library` the method was published with) is the default.
Another connector is chosen by name with `HARNESS2_MEMORY_CONNECTOR` and registered with
`register()`. Whatever the connector, entries stay per (run, mode, domain), and the schedule
writes through `store()`, which runs the grader-verdict check before any connector sees them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence, runtime_checkable

from . import context
from . import tree as harness_tree
from .library import Library, has_experience
from .tree import SurfaceSpec

ENV_VAR = "HARNESS2_MEMORY_CONNECTOR"
DEFAULT = "file"


@runtime_checkable
class MemoryConnector(Protocol):
    """One (run, mode, domain) library as the schedule uses it."""

    domain: str

    def entries(self) -> list[str]: ...

    def retain(self, task_ids: Sequence[str]) -> None: ...

    def add(
        self,
        task_id: str,
        harness: harness_tree.Tree,
        *,
        why: str = "",
        label: str = "",
        run_id: str = "",
        experience: dict | None = None,
    ) -> Path | None: ...

    def context_dir(self) -> Path | None: ...


FileConnector = Library

Factory = Callable[..., MemoryConnector]
"""`factory(root=..., domain=..., surface=...)` -> a connector for that library."""

_FACTORIES: dict[str, Factory] = {DEFAULT: FileConnector}


def register(name: str, factory: Factory) -> None:
    """Make `factory` selectable as `HARNESS2_MEMORY_CONNECTOR=<name>`."""
    if not name or name != name.strip():
        raise ValueError(f"connector name must be non-empty with no surrounding spaces: {name!r}")
    _FACTORIES[name] = factory


def names() -> list[str]:
    return sorted(_FACTORIES)


def connector_for(
    root: Path | str,
    domain: str,
    surface: SurfaceSpec,
    *,
    name: str | None = None,
    env: Mapping[str, str] | None = None,
) -> MemoryConnector:
    """The connector for one library: `name`, else `HARNESS2_MEMORY_CONNECTOR`, else file."""
    env = os.environ if env is None else env
    chosen = (name or env.get(ENV_VAR) or DEFAULT).strip()
    factory = _FACTORIES.get(chosen)
    if factory is None:
        raise ValueError(f"unknown memory connector {chosen!r}; known: {', '.join(names())}")
    conn = factory(root=Path(root), domain=domain, surface=surface)
    if not isinstance(conn, MemoryConnector):
        raise TypeError(
            f"memory connector {chosen!r} returned {type(conn).__name__}, not a MemoryConnector"
        )
    return conn


def store(
    conn: MemoryConnector,
    task_id: str,
    harness: harness_tree.Tree,
    *,
    why: str = "",
    label: str = "",
    run_id: str = "",
    experience: dict | None = None,
) -> Path | None:
    """Add one adopted harness through `conn`, after the checks every connector must honor.

    Entries without a complete experience are skipped, and a tree or experience that carries
    a grader verdict raises before anything reaches the connector.
    """
    if not has_experience(experience):
        return None
    context.assert_tree_has_no_grader_verdicts(harness)
    context.assert_tree_has_no_grader_verdicts({"EXPERIENCE.md": str(experience) + why})
    return conn.add(task_id, harness, why=why, label=label, run_id=run_id, experience=experience)
