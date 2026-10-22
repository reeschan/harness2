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
`entries()`, `retain()`, `add()` and `context_dir()`. A connector may also define an optional
`close()`; the public `close` calls it when present, so a backend holding a connection, a
session or a file can release it. Connectors are found by name.
`connector_name` picks the name: an explicit name, then the `HARNESS2_MEMORY_CONNECTOR`
environment variable, then the default `file`. `resolve_factory` turns the name into a
factory: a built-in registration (`register`; only the bundled `file` connector by default),
then an entry point of the same name in the group `harness2.memory_connectors`, then, for a
name containing `:`, a `module:Attr` import path. An entry point's `load()` and a
`module:Attr` target are both factories: callables called as `factory(root=Path,
domain=str, surface=SurfaceSpec, **options)` that return a MemoryConnector. `options` holds
the connector-specific settings; `check_options` checks their names against the factory's
signature before a run starts. `store` runs the grader-verdict checks before handing an
experience to any connector, so the checks hold whatever the backend does.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from . import context, library
from . import recall as recall_block
from . import tree as harness_tree
from .library import Library
from .tree import SurfaceSpec


@runtime_checkable
class MemoryConnector(Protocol):
    """One (run, domain) memory: read before each task, appended after.

    `close()` is optional: a connector that holds a resource may define it to release the
    resource, and `memory.close` calls it after the domain's tasks. A connector without one is
    still a MemoryConnector.
    """

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
ENTRY_POINT_GROUP = "harness2.memory_connectors"

_RESERVED_OPTIONS = ("root", "domain", "surface")

Factory = Callable[..., MemoryConnector]

_REGISTRY: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    """Register a connector factory under `name`; an existing entry of that name is replaced.

    The factory is called as `factory(root=Path, domain=str, surface=SurfaceSpec)`.
    """
    _REGISTRY[name] = factory


def _entry_points() -> list[Any]:
    """The installed entry points of `ENTRY_POINT_GROUP`."""
    return list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def names() -> list[str]:
    """The built-in registered names plus the entry point names of `ENTRY_POINT_GROUP`,
    sorted and without duplicates."""
    return sorted(set(_REGISTRY) | {ep.name for ep in _entry_points()})


def connector_name(*, name: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """The connector name: `name` when given, else `ENV_VAR` from `env` (or `os.environ` when
    `env` is None), else `DEFAULT`. `resolve_factory` turns the name into a factory.
    """
    if name is not None:
        return name
    return (os.environ if env is None else env).get(ENV_VAR) or DEFAULT


def _import_factory(name: str) -> Factory:
    """Load the factory named by a `module:Attr` import path; Attr may be a dotted chain.

    A malformed path, a failed import and a missing attribute raise ValueError; an attribute
    that is not callable raises TypeError.
    """
    module_name, _, attr_path = name.partition(":")
    if name.count(":") != 1 or not module_name or not attr_path:
        raise ValueError(
            f"memory connector {name!r} is not a valid import path; expected 'module:Attr'"
        )
    try:
        target = importlib.import_module(module_name)
    except Exception as exc:
        raise ValueError(
            f"memory connector {name!r}: cannot import module {module_name!r}: {exc}"
        ) from exc
    for attr in attr_path.split("."):
        try:
            target = getattr(target, attr)
        except AttributeError as exc:
            raise ValueError(
                f"memory connector {name!r}: {module_name!r} has no attribute {attr_path!r}"
            ) from exc
    if not callable(target):
        raise TypeError(
            f"memory connector {name!r} is {type(target).__name__}, which is not callable"
        )
    return target


def _load_entry_point(name: str, ep: Any) -> Factory:
    """Load an entry point as a factory; a failed load raises ValueError, a target that is not
    callable raises TypeError."""
    try:
        target = ep.load()
    except Exception as exc:
        raise ValueError(
            f"memory connector {name!r}: cannot load entry point {ENTRY_POINT_GROUP}/{name}: {exc}"
        ) from exc
    if not callable(target):
        raise TypeError(
            f"memory connector {name!r} entry point is {type(target).__name__}, "
            "which is not callable"
        )
    return target


def resolve_factory(name: str) -> Factory:
    """The factory for the connector `name`.

    Looked up in this order: the built-in registrations (`register`), an entry point named
    `name` in the group `ENTRY_POINT_GROUP` (`harness2.memory_connectors`), and, for a name
    containing `:`, a `module:Attr` import path (see `_import_factory`). An entry point's
    `load()` must return a factory, the same as a `module:Attr` target: a callable called as
    `factory(root=Path, domain=str, surface=SurfaceSpec)`. A name found nowhere raises
    ValueError listing the registered and entry point names.
    """
    factory = _REGISTRY.get(name)
    if factory is not None:
        return factory
    eps = _entry_points()
    for ep in eps:
        if ep.name == name:
            return _load_entry_point(name, ep)
    if ":" in name:
        return _import_factory(name)
    registered = ", ".join(sorted(_REGISTRY)) or "(none)"
    entry_names = ", ".join(sorted({ep.name for ep in eps})) or "(none)"
    raise ValueError(
        f"unknown memory connector {name!r}; registered: {registered}; "
        f"entry points ({ENTRY_POINT_GROUP}): {entry_names}"
    )


def connector_for(
    root: Path | str,
    domain: str,
    surface: SurfaceSpec,
    *,
    name: str | None = None,
    env: Mapping[str, str] | None = None,
    options: Mapping[str, Any] | None = None,
) -> MemoryConnector:
    """Build the connector for one (root, domain).

    The name comes from `connector_name` and the factory from `resolve_factory` (built-in
    registration, then entry point, then `module:Attr`); the factory is called as
    `factory(root=Path, domain=str, surface=SurfaceSpec, **options)`. `options` carries the
    connector-specific settings and must not set `root`, `domain` or `surface`: harness2
    supplies those, and a factory cannot override them. A name found nowhere raises ValueError;
    a factory result that is not a MemoryConnector raises TypeError.
    """
    name = connector_name(name=name, env=env)
    factory = resolve_factory(name)
    extra = dict(options or {})
    reserved = sorted(key for key in extra if key in _RESERVED_OPTIONS)
    if reserved:
        raise ValueError(
            f"memory connector {name!r} options must not set {reserved}; "
            "harness2 supplies root, domain and surface itself"
        )
    conn = factory(root=Path(root), domain=domain, surface=surface, **extra)
    if not isinstance(conn, MemoryConnector):
        raise TypeError(
            f"memory connector {name!r} factory returned {type(conn).__name__}, "
            f"which is not a MemoryConnector"
        )
    return conn


def check_options(name: str, options: Mapping[str, Any]) -> None:
    """Check that the factory of connector `name` accepts every key in `options`.

    `resolve_factory` finds the factory and `inspect.signature` reads its parameters: the keys
    must name parameters the factory can take as keywords (a `**kwargs` parameter takes
    anything). A key the factory cannot take raises ValueError naming the connector and the
    keys, sorted. `root`, `domain` and `surface` raise ValueError too, because harness2 supplies
    them. A factory whose signature cannot be read is not checked: it may accept anything.
    """
    reserved = sorted(key for key in options if key in _RESERVED_OPTIONS)
    if reserved:
        raise ValueError(
            f"memory connector {name!r} options must not set {reserved}; "
            "harness2 supplies root, domain and surface itself"
        )
    factory = resolve_factory(name)
    try:
        params = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return
    if any(p.kind is p.VAR_KEYWORD for p in params):
        return
    accepted = {p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    unknown = sorted(set(options) - accepted)
    if unknown:
        raise ValueError(
            f"memory connector {name!r} factory does not accept option(s) {unknown}; "
            f"it takes {sorted(accepted)}"
        )


def close(conn: MemoryConnector) -> None:
    """Call the connector's optional `close()`, once, to release what it holds.

    A connector without `close`, or with `close` set to None, needs no cleanup and is left
    alone. A `close` that is present but not callable raises TypeError, so a broken connector
    fails loudly instead of being skipped.
    """
    fn = getattr(conn, "close", None)
    if fn is None:
        return
    if not callable(fn):
        raise TypeError(f"memory connector close must be a method or None, got {type(fn).__name__}")
    fn()


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

    An incomplete experience stores nothing and returns None. Otherwise every recall block is
    stripped from the tree, the stripped tree and the experience text are checked for grader
    verdicts, and `conn.add` is called with the stripped tree; its result is returned.
    """
    if not library.has_experience(experience):
        return None
    harness = recall_block.strip(harness)
    context.assert_tree_has_no_grader_verdicts(harness)
    context.assert_tree_has_no_grader_verdicts({"EXPERIENCE.md": str(experience) + why})
    return conn.add(task_id, harness, why=why, label=label, run_id=run_id, experience=experience)


def recall(conn: MemoryConnector, task_id: str, limit_chars: int) -> str:
    """The connector's recall text for `task_id`, cut to `limit_chars`.

    Recall is optional: a connector without a callable `recall`, or a `limit_chars` of zero or
    less, gives "".
    """
    fn = getattr(conn, "recall", None)
    if not callable(fn) or limit_chars <= 0:
        return ""
    return (fn(task_id, limit_chars) or "")[:limit_chars]


register(DEFAULT, FileConnector)
