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

"""Memory connectors: interchangeable backends for the per-domain experience library.

A connector is anything with the surface the scheduler uses on a library: `domain`,
`entries()`, `retain()`, `add()` and `context_dir()`. Connectors are registered by name;
`connector_for` picks one by explicit name, then by the `HARNESS2_MEMORY_CONNECTOR`
environment variable, then the on-disk default `file`. `store` runs the grader-verdict
checks before handing an experience to any connector, so the checks hold whatever the
backend does.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence, runtime_checkable

from . import context, library
from . import tree as harness_tree
from .library import Library
from .tree import SurfaceSpec


@runtime_checkable
class MemoryConnector(Protocol):
    """One (run, domain) memory: read before each task, appended after."""

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

ENV_VAR = "HARNESS2_MEMORY_CONNECTOR"
DEFAULT = "file"

Factory = Callable[..., MemoryConnector]

_REGISTRY: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    """Register a connector factory under `name`; an existing entry of that name is replaced.

    The factory is called as `factory(root=Path, domain=str, surface=SurfaceSpec)`.
    """
    _REGISTRY[name] = factory


def names() -> list[str]:
    """The registered connector names, sorted."""
    return sorted(_REGISTRY)


def connector_for(
    root: Path | str,
    domain: str,
    surface: SurfaceSpec,
    *,
    name: str | None = None,
    env: Mapping[str, str] | None = None,
) -> MemoryConnector:
    """Build the connector for one (root, domain).

    The name is `name` when given, else `ENV_VAR` from `env` (or `os.environ` when `env` is
    None), else `DEFAULT`. An unregistered name raises ValueError; a factory result that is
    not a MemoryConnector raises TypeError.
    """
    if name is None:
        name = (os.environ if env is None else env).get(ENV_VAR) or DEFAULT
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ValueError(
            f"unknown memory connector {name!r}; registered: {', '.join(names()) or '(none)'}"
        )
    conn = factory(root=Path(root), domain=domain, surface=surface)
    if not isinstance(conn, MemoryConnector):
        raise TypeError(
            f"memory connector {name!r} factory returned {type(conn).__name__}, "
            f"which is not a MemoryConnector"
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
    """Hand one adopted harness and its experience to `conn`.

    An incomplete experience stores nothing and returns None. Otherwise the tree and the
    experience text are checked for grader verdicts before `conn.add` is called, and its
    result is returned.
    """
    if not library.has_experience(experience):
        return None
    context.assert_tree_has_no_grader_verdicts(harness)
    context.assert_tree_has_no_grader_verdicts({"EXPERIENCE.md": str(experience) + why})
    return conn.add(task_id, harness, why=why, label=label, run_id=run_id, experience=experience)


register(DEFAULT, FileConnector)
