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

"""Task intake: tasks that arrive from outside a benchmark, and the connectors that carry them.

A `RawTask` is a task as a connector received it; a `TaskSpec` is the same task normalized
into what the scheduler needs. `TASK_SPEC_SCHEMA` describes the dict form of a TaskSpec and
`validate_task_spec` checks a dict against it with a small standard-library interpreter of a
JSON Schema subset, plus the checks the schema cannot express (BenchPrompts' own rules and
relative-path rules for files and the deliverable location).

The task connector contract
---------------------------

A `TaskConnector` polls for raw tasks, fetches their attachments, and takes back either a
deliverable or a failure. These four methods are required:

- `poll() -> Iterable[RawTask]`
- `fetch_files(raw, dest) -> None`
- `deliver(task_id, deliverable_dir, summary) -> None`
- `fail(task_id, reason) -> None`

A connector may also offer optional capabilities, read by `capabilities(connector)` with
getattr; anything missing gets a default:

- attribute `workspace_policy`: a `WorkspacePolicy` (paths left out of the diff and a guard
  run before the diff goes back to the source) or None. Default: `WorkspacePolicy()`, which
  leaves nothing out and checks nothing.
- attribute `scrub_env`: an iterable of environment variable names removed from the agent's
  environment, or None. Default: `()`.
- method `judge()`: returns a `Judge` or None. A Judge is called as
  `judge(run_dir, task_id, criteria)` and returns None or a dict with the keys the harness's
  scoring record reads: `n_passed` (int), `n_criteria` (int) and `all_pass` (bool); any other
  keys are kept as raw data. Default: None (no judge).
- method `report(task_id)`: returns the text to print for a task, or None. Default: None.
- method `close()`: releases what the connector holds. Default: a function that does nothing.

Connectors are found by name. `connector_name` picks the name: an explicit name, then the
`HARNESS2_TASK_CONNECTOR` environment variable, then the default `local`. `resolve_factory`
turns the name into a factory: a built-in registration (`register`; only the bundled `local`
connector by default), then an entry point of the same name in the group
`harness2.task_connectors`, then, for a name containing `:`, a `module:Attr` import path. An
entry point's `load()` and a `module:Attr` target are both factories: callables that take the
connector options as keyword arguments (`factory(**options)`) and return a TaskConnector.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from .spec import BenchPrompts


@dataclass(frozen=True)
class RawTask:
    """One task as a connector received it, before normalization."""

    id: str
    title: str = ""
    body: str = ""
    files: tuple[str, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class TaskSpec:
    """One normalized task: what the scheduler needs to run it.

    `files` lists attachment paths relative to the task's files directory; an empty tuple
    means every attachment. `deliverable_where` is a path relative to `output/` ("" is
    `output/` itself). `private_criteria` is never shown to the agent.
    """

    task_id: str
    domain: str
    prompt: str
    files: tuple[str, ...]
    deliverable_what: str
    deliverable_where: str = ""
    prompts: BenchPrompts
    private_criteria: tuple[str, ...] = ()


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"

TASK_SPEC_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "pattern": _ID_PATTERN},
        "domain": {"type": "string", "pattern": _ID_PATTERN},
        "prompt": {"type": "string", "minLength": 1},
        "files": {"type": "array", "items": {"type": "string"}},
        "deliverable": {
            "type": "object",
            "properties": {
                "what": {"type": "string"},
                "where": {"type": "string"},
            },
            "required": ["what"],
            "additionalProperties": False,
        },
        "prompts": {
            "type": "object",
            "properties": {
                "work": {"type": "string"},
                "domain_word": {"type": "string"},
                "deliverable_word": {"type": "string"},
                "yardstick": {"type": "string"},
            },
            "required": ["work"],
            "additionalProperties": False,
        },
        "private_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["task_id", "domain", "prompt", "files", "deliverable", "prompts"],
    "additionalProperties": False,
}

_PROMPT_KEYS = ("work", "domain_word", "deliverable_word", "yardstick")


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValueError(f"unsupported schema type {expected!r}")


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _pattern_matches(pattern: str, value: str) -> bool:
    # A trailing `$` anchors at the very end, as in ECMA-262 (Python's `$` also matches
    # before a final newline).
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.search(pattern, value) is not None


def _check(value: Any, schema: Mapping[str, Any], where: str, out: list[str]) -> None:
    """Append one sentence per problem of `value` against `schema` (the supported subset)."""
    label = where or "the task spec"
    expected = schema.get("type")
    if expected is not None and not _type_ok(value, expected):
        out.append(f"{label} must be a {expected}, got {_json_type(value)}.")
        return
    if isinstance(value, str):
        min_len = schema.get("minLength")
        if min_len is not None and len(value) < min_len:
            out.append(f"{label} must be at least {min_len} character(s) long.")
        pattern = schema.get("pattern")
        if pattern is not None and not _pattern_matches(pattern, value):
            out.append(f"{label} {value!r} does not match the pattern {pattern!r}.")
    if isinstance(value, (list, tuple)) and "items" in schema:
        for i, item in enumerate(value):
            _check(item, schema["items"], f"{where}[{i}]", out)
    if isinstance(value, Mapping):
        props = schema.get("properties", {})
        prefix = f"{where}." if where else ""
        for key in schema.get("required", ()):
            if key not in value:
                out.append(f"{label} is missing the required key {key!r}.")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    out.append(f"{label} has the unexpected key {key!r}.")
        for key, sub in props.items():
            if key in value:
                _check(value[key], sub, f"{prefix}{key}", out)


def _path_problem(label: str, path: str) -> str | None:
    if "\x00" in path:
        return f"{label} {path!r} must not contain a NUL character."
    if any(ch < " " for ch in path):
        return f"{label} {path!r} must not contain a control character."
    if "\\" in path:
        return f"{label} {path!r} contains a backslash; use '/' as the separator."
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return f"{label} {path!r} is an absolute path; it must be relative."
    if ".." in path.split("/"):
        return f"{label} {path!r} contains '..'; it must stay inside its directory."
    return None


def validate_task_spec(obj: Any) -> list[str]:
    """The problems of `obj` as the dict form of a TaskSpec, one English sentence each.

    An empty list means `obj` passes. Besides `TASK_SPEC_SCHEMA`, the `prompts` object must
    build a BenchPrompts, and every `files` entry and `deliverable.where` must be a relative
    path with '/' separators, no '..' segment, and no NUL or other control character.
    """
    problems: list[str] = []
    _check(obj, TASK_SPEC_SCHEMA, "", problems)
    if not isinstance(obj, Mapping):
        return problems

    files = obj.get("files")
    if isinstance(files, (list, tuple)):
        for i, item in enumerate(files):
            if isinstance(item, str):
                problem = _path_problem(f"files[{i}]", item)
                if problem:
                    problems.append(problem)

    deliverable = obj.get("deliverable")
    if isinstance(deliverable, Mapping):
        where = deliverable.get("where")
        if isinstance(where, str) and where:
            problem = _path_problem("deliverable.where", where)
            if problem:
                problems.append(problem)

    prompts = obj.get("prompts")
    if isinstance(prompts, Mapping) and isinstance(prompts.get("work"), str):
        kwargs = {k: prompts[k] for k in _PROMPT_KEYS if k in prompts}
        if all(isinstance(v, str) for v in kwargs.values()):
            try:
                BenchPrompts(**kwargs)
            except ValueError as exc:
                problems.append(f"prompts: {exc}")
    return problems


def task_spec_from_dict(obj: Mapping[str, Any]) -> TaskSpec:
    """Build a TaskSpec from its dict form; any problem from `validate_task_spec` raises
    ValueError listing every problem, one per line.
    """
    problems = validate_task_spec(obj)
    if problems:
        raise ValueError("invalid task spec:\n" + "\n".join(f"- {p}" for p in problems))
    prompts = obj["prompts"]
    deliverable = obj["deliverable"]
    return TaskSpec(
        task_id=obj["task_id"],
        domain=obj["domain"],
        prompt=obj["prompt"],
        files=tuple(obj["files"]),
        deliverable_what=deliverable["what"],
        deliverable_where=deliverable.get("where", ""),
        prompts=BenchPrompts(**{k: prompts[k] for k in _PROMPT_KEYS if k in prompts}),
        private_criteria=tuple(obj.get("private_criteria", ())),
    )


def task_spec_to_dict(spec: TaskSpec, *, include_private: bool) -> dict[str, Any]:
    """The dict form of `spec`, with every optional key filled in.

    `include_private=False` leaves out the `private_criteria` key. With `include_private=True`
    the result round-trips through `task_spec_from_dict` to an equal TaskSpec.
    """
    out: dict[str, Any] = {
        "task_id": spec.task_id,
        "domain": spec.domain,
        "prompt": spec.prompt,
        "files": list(spec.files),
        "deliverable": {"what": spec.deliverable_what, "where": spec.deliverable_where},
        "prompts": {k: getattr(spec.prompts, k) for k in _PROMPT_KEYS},
    }
    if include_private:
        out["private_criteria"] = list(spec.private_criteria)
    return out


@runtime_checkable
class TaskConnector(Protocol):
    """Where tasks come from and where their results go.

    These four methods are the whole required contract; the optional capabilities are read by
    `capabilities` (see the module docstring).
    """

    def poll(self) -> Iterable[RawTask]: ...

    def fetch_files(self, raw: RawTask, dest: Path) -> None: ...

    def deliver(self, task_id: str, deliverable_dir: Path, summary: Mapping) -> None: ...

    def fail(self, task_id: str, reason: str) -> None: ...


Judge = Callable[[Path, str, Sequence[str]], dict | None]
"""A connector's judge: `judge(run_dir, task_id, criteria)` returns None or a dict with the keys
the harness's scoring record reads (`n_passed`, `n_criteria`, `all_pass`); other keys stay raw."""

WorkspaceGuard = Callable[[Path, Path], str | None]
"""`guard(worktree, source)`: None when the diff may go back to the source, else the reason."""


@dataclass(frozen=True)
class WorkspacePolicy:
    """How a task's worktree diff goes back to the source.

    `exclude` lists paths, relative to the source root, that are left out of the diff. `guard`
    is called as `guard(worktree, source)` before the diff goes back; it returns None when that
    is fine, else the reason it is not. The default leaves nothing out and checks nothing.
    """

    exclude: tuple[str, ...] = ()
    guard: WorkspaceGuard | None = None


def _noop() -> None:
    return None


@dataclass(frozen=True)
class Capabilities:
    """A connector's optional capabilities, with the defaults filled in (see `capabilities`)."""

    workspace_policy: WorkspacePolicy = field(default_factory=WorkspacePolicy)
    scrub_env: tuple[str, ...] = ()
    judge: Judge | None = None
    report: Callable[[str], str | None] | None = None
    close: Callable[[], None] = _noop


def _type_name(value: Any) -> str:
    return type(value).__name__


def capabilities(connector: Any) -> Capabilities:
    """Read the optional capabilities of `connector` with getattr, filling in the defaults.

    - `workspace_policy` (attribute): a WorkspacePolicy or None; default `WorkspacePolicy()`.
    - `scrub_env` (attribute): an iterable of str (not a single str) or None; default `()`.
    - `judge()` (method): returns a Judge or None; default None. It is called here, once.
      A Judge returns None or a dict with `n_passed`, `n_criteria` and `all_pass`.
    - `report(task_id)` (method): returns the text for a task or None; default None.
    - `close()` (method): default a function that does nothing.

    An attribute that is present but None counts as missing. A value of the wrong type raises
    TypeError naming the capability and the type it had.
    """
    policy = getattr(connector, "workspace_policy", None)
    if policy is None:
        policy = WorkspacePolicy()
    elif not isinstance(policy, WorkspacePolicy):
        raise TypeError(
            f"task connector capability 'workspace_policy' must be a WorkspacePolicy or None, "
            f"got {_type_name(policy)}"
        )
    if not isinstance(policy.exclude, tuple) or not all(isinstance(p, str) for p in policy.exclude):
        raise TypeError(
            "task connector capability 'workspace_policy.exclude' must be a tuple of str, "
            f"got {_type_name(policy.exclude)}"
        )
    if policy.guard is not None and not callable(policy.guard):
        raise TypeError(
            "task connector capability 'workspace_policy.guard' must be callable or None, "
            f"got {_type_name(policy.guard)}"
        )

    raw_env = getattr(connector, "scrub_env", None)
    if raw_env is None:
        scrub_env: tuple[str, ...] = ()
    else:
        if isinstance(raw_env, (str, bytes)) or not isinstance(raw_env, Iterable):
            raise TypeError(
                "task connector capability 'scrub_env' must be an iterable of str or None, "
                f"got {_type_name(raw_env)}"
            )
        scrub_env = tuple(raw_env)
        for item in scrub_env:
            if not isinstance(item, str):
                raise TypeError(
                    "task connector capability 'scrub_env' must contain only str, "
                    f"got {_type_name(item)}"
                )

    judge_method = getattr(connector, "judge", None)
    judge: Judge | None = None
    if judge_method is not None:
        if not callable(judge_method):
            raise TypeError(
                "task connector capability 'judge' must be a method returning a Judge or None, "
                f"got {_type_name(judge_method)}"
            )
        judge = judge_method()
        if judge is not None and not callable(judge):
            raise TypeError(
                "task connector capability 'judge()' must return a callable Judge or None, "
                f"got {_type_name(judge)}"
            )

    report = getattr(connector, "report", None)
    if report is not None and not callable(report):
        raise TypeError(
            f"task connector capability 'report' must be a method or None, got {_type_name(report)}"
        )

    close = getattr(connector, "close", None)
    if close is None:
        close = _noop
    elif not callable(close):
        raise TypeError(
            f"task connector capability 'close' must be a method or None, got {_type_name(close)}"
        )

    return Capabilities(
        workspace_policy=policy,
        scrub_env=scrub_env,
        judge=judge,
        report=report,
        close=close,
    )


ENV_VAR = "HARNESS2_TASK_CONNECTOR"
DEFAULT = "local"
ENTRY_POINT_GROUP = "harness2.task_connectors"

Factory = Callable[..., TaskConnector]

_REGISTRY: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    """Register a connector factory under `name`; an existing entry of that name is replaced.

    The factory is called as `factory(**options)` with the options given to `connector_for`.
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
    `env` is None), else `DEFAULT`.
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
            f"task connector {name!r} is not a valid import path; expected 'module:Attr'"
        )
    try:
        target = importlib.import_module(module_name)
    except Exception as exc:
        raise ValueError(
            f"task connector {name!r}: cannot import module {module_name!r}: {exc}"
        ) from exc
    for attr in attr_path.split("."):
        try:
            target = getattr(target, attr)
        except AttributeError as exc:
            raise ValueError(
                f"task connector {name!r}: {module_name!r} has no attribute {attr_path!r}"
            ) from exc
    if not callable(target):
        raise TypeError(
            f"task connector {name!r} is {type(target).__name__}, which is not callable"
        )
    return target


def _load_entry_point(name: str, ep: Any) -> Factory:
    """Load an entry point as a factory; a failed load raises ValueError, a target that is not
    callable raises TypeError."""
    try:
        target = ep.load()
    except Exception as exc:
        raise ValueError(
            f"task connector {name!r}: cannot load entry point {ENTRY_POINT_GROUP}/{name}: {exc}"
        ) from exc
    if not callable(target):
        raise TypeError(
            f"task connector {name!r} entry point is {type(target).__name__}, which is not callable"
        )
    return target


def resolve_factory(name: str) -> Factory:
    """The factory for the connector `name`.

    Looked up in this order: the built-in registrations (`register`), an entry point named
    `name` in the group `ENTRY_POINT_GROUP` (`harness2.task_connectors`), and, for a name
    containing `:`, a `module:Attr` import path (see `_import_factory`). An entry point's
    `load()` must return a factory, the same as a `module:Attr` target: a callable taking the
    connector options as keyword arguments and returning a TaskConnector. A name found nowhere
    raises ValueError listing the registered and entry point names.
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
        f"unknown task connector {name!r}; registered: {registered}; "
        f"entry points ({ENTRY_POINT_GROUP}): {entry_names}"
    )


def connector_for(
    *,
    name: str | None = None,
    env: Mapping[str, str] | None = None,
    **options: Any,
) -> TaskConnector:
    """Build a task connector.

    The name comes from `connector_name` and the factory from `resolve_factory` (built-in
    registration, then entry point, then `module:Attr`); the factory is called as
    `factory(**options)`. A name found nowhere raises ValueError; a factory result that is not
    a TaskConnector raises TypeError.
    """
    name = connector_name(name=name, env=env)
    factory = resolve_factory(name)
    conn = factory(**options)
    if not isinstance(conn, TaskConnector):
        raise TypeError(
            f"task connector {name!r} factory returned {type(conn).__name__}, "
            f"which is not a TaskConnector"
        )
    return conn


def _local_factory(**options: Any) -> TaskConnector:
    """Import the local file connector on first use and build it with `options`."""
    return _import_factory("harness2.core.intake_local:LocalFileConnector")(**options)


register(DEFAULT, _local_factory)
