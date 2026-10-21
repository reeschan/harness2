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

A `TaskConnector` polls for raw tasks, fetches their attachments, and takes back either a
deliverable or a failure. Connectors are registered by name; `connector_for` picks one by
explicit name, then by the `HARNESS2_TASK_CONNECTOR` environment variable, then the default
`local`. A name that is not registered but has the form `module:Attr` is imported and used as
the factory.
"""

from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

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
    """Where tasks come from and where their results go."""

    def poll(self) -> Iterable[RawTask]: ...

    def fetch_files(self, raw: RawTask, dest: Path) -> None: ...

    def deliver(self, task_id: str, deliverable_dir: Path, summary: Mapping) -> None: ...

    def fail(self, task_id: str, reason: str) -> None: ...


ENV_VAR = "HARNESS2_TASK_CONNECTOR"
DEFAULT = "local"

Factory = Callable[..., TaskConnector]

_REGISTRY: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    """Register a connector factory under `name`; an existing entry of that name is replaced.

    The factory is called as `factory(**options)` with the options given to `connector_for`.
    """
    _REGISTRY[name] = factory


def names() -> list[str]:
    """The registered connector names, sorted."""
    return sorted(_REGISTRY)


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


def connector_for(
    *,
    name: str | None = None,
    env: Mapping[str, str] | None = None,
    **options: Any,
) -> TaskConnector:
    """Build a task connector.

    The name comes from `connector_name`. A registered name is used first; otherwise a name
    containing `:` is loaded as a `module:Attr` import path (see `_import_factory`). Any other
    unregistered name raises ValueError; a factory result that is not a TaskConnector raises
    TypeError.
    """
    name = connector_name(name=name, env=env)
    factory = _REGISTRY.get(name)
    if factory is None:
        if ":" not in name:
            raise ValueError(
                f"unknown task connector {name!r}; registered: {', '.join(names()) or '(none)'}"
            )
        factory = _import_factory(name)
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


def _jev_mcp_factory(**options: Any) -> TaskConnector:
    """Import the jev-mcp connector on first use and build it with `options`."""
    return _import_factory("harness2.core.intake_mcp:connector_from_options")(**options)


register(DEFAULT, _local_factory)
register("jev-mcp", _jev_mcp_factory)
