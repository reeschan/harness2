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

"""Model connectors: interchangeable sources of the models the executor and improver use.

A connector is anything with `routes(role, *, task_id, substrate)`, which returns the
`ModelChoice` candidates for a role, best first. Everything else is optional and read by
`capabilities`: `run_session(role, request)` (run a whole session itself), `fences()` (the
fence it applies to each runner it can execute), `record(role, task_id, choice, outcome)` and
`close()`. A connector that defines `run_session` must also define `fences`, so harness2 can
check the fence before it hands the session over: `fenced_runners` keeps only the runners
whose declared fence meets `IMPROVER_FENCE`.

`run_session` runs only on the runner and model of `request.choice`, the candidate harness2
picked. If `SessionResult.runner` is not empty and differs from `choice.runner`, harness2 treats
the result as a failure.

Connectors are found by name, in the same way as the memory connectors. `connector_name`
picks the name: an explicit name, then the `HARNESS2_MODEL_CONNECTOR` environment variable,
then the default `env`. `resolve_factory` turns the name into a factory: a built-in
registration (`register`), then an entry point of the same name in the group
`harness2.model_connectors`, then, for a name containing `:`, a `module:Attr` import path.
The factory is called as `factory(**options)`; `check_options` checks the option names
against its signature before a run starts.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .. import config


class Role:
    """The roles a model is chosen for."""

    EXECUTOR = "executor"
    IMPROVER = "improver"


ROLES = (Role.EXECUTOR, Role.IMPROVER)


@dataclass(frozen=True)
class ModelChoice:
    """One candidate: the runner that executes it, the model ID and an optional effort."""

    runner: str
    model: str
    effort: str = ""


@dataclass(frozen=True)
class SessionRequest:
    """What a connector's `run_session` is asked to do.

    `choice` is the candidate harness2 picked: the session runs on its runner and model only.
    """

    cwd: Path
    mission: str
    system: str
    wall: int
    label: str
    choice: ModelChoice


@dataclass(frozen=True)
class Tokens:
    """Token counts; None when the backend does not report them."""

    input: int | None = None
    output: int | None = None


@dataclass(frozen=True)
class SessionResult:
    """What a connector's `run_session` returns.

    `runner` is the runner that actually ran the session; empty means `request.choice.runner`.
    """

    text: str
    ok: bool
    cost_usd: float = 0.0
    tokens: Tokens = Tokens()
    wall_s: float = 0.0
    error: str = ""
    unavailable: bool = False
    runner: str = ""


@dataclass(frozen=True)
class Outcome:
    """How one use of a choice ended, as handed to a connector's `record`."""

    ok: bool
    unavailable: bool = False
    cost_usd: float = 0.0
    tokens: Tokens = Tokens()
    wall_s: float = 0.0
    error: str = ""
    run_id: str = ""
    label: str = ""


# The kinds of tool a session can have, as a fence declaration names them (runner-neutral).
CAPABILITIES = ("read", "write", "search", "commands", "network", "mcp")


@dataclass(frozen=True)
class FenceDeclaration:
    """The fence a connector says it applies to the sessions it executes, in runner-neutral terms.

    - `capabilities`: the kinds of tool the session has, each one of `CAPABILITIES`: "read"
      (read files), "write" (write or edit files), "search" (find files or text), "commands"
      (run commands), "network" (reach outside, e.g. fetch from the Web) and "mcp" (tools from
      MCP servers).
    - `workdir_only`: the session cannot reach files outside the working directory it is given
      (no other directory is added).
    - `asks_human`: the session may stop to ask a person for confirmation.
    - `harness_credentials`: harness2's own API credentials (environment variables) reach the
      child process.
    - `runner`: the name of the runner the connector uses for this fence. It is for the record
      and for tying a candidate (`ModelChoice.runner`) to the declaration; it is never checked.
    """

    capabilities: frozenset[str]
    workdir_only: bool
    asks_human: bool
    harness_credentials: bool
    runner: str = ""


@dataclass(frozen=True)
class FenceLimit:
    """The most a declared fence may allow: the ceiling of `capabilities`, and the values the
    other three fields must have."""

    capabilities: frozenset[str]
    workdir_only: bool
    asks_human: bool
    harness_credentials: bool


# The one condition harness2 puts on an improver session run by a connector: file tools only,
# nothing outside the working directory, no MCP, no prompts, no harness credentials.
IMPROVER_FENCE = FenceLimit(
    capabilities=frozenset({"read", "write", "search"}),
    workdir_only=True,
    asks_human=False,
    harness_credentials=False,
)


def fence_problems(declaration: Any) -> list[str]:
    """Why `declaration` does not meet `IMPROVER_FENCE`; an empty list means it does.

    The declaration must be a `FenceDeclaration` with well-typed fields. Its `capabilities` must
    be values of `CAPABILITIES` and a subset of the allowed ones, `workdir_only` must be true,
    `asks_human` and `harness_credentials` must be false. Its `runner` is not checked, beyond
    being a str.
    """
    if not isinstance(declaration, FenceDeclaration):
        return [f"declaration must be a FenceDeclaration, got {type(declaration).__name__}"]
    problems: list[str] = []
    caps = declaration.capabilities
    if not isinstance(caps, (tuple, list, set, frozenset)):
        problems.append(
            f"capabilities must be a tuple, list, set or frozenset of str, got {type(caps).__name__}"
        )
    elif not all(isinstance(item, str) for item in caps):
        problems.append("capabilities must contain only str")
    else:
        unknown = sorted(set(caps) - set(CAPABILITIES))
        if unknown:
            problems.append(f"capabilities outside {list(CAPABILITIES)}: {unknown}")
        extra = sorted((set(caps) & set(CAPABILITIES)) - IMPROVER_FENCE.capabilities)
        if extra:
            problems.append(f"capabilities beyond the allowed set: {extra}")
    for name in ("workdir_only", "asks_human", "harness_credentials"):
        value = getattr(declaration, name)
        wanted = getattr(IMPROVER_FENCE, name)
        if not isinstance(value, bool):
            problems.append(f"{name} must be a bool, got {type(value).__name__}")
        elif value is not wanted:
            problems.append(f"{name} must be {wanted}")
    if not isinstance(declaration.runner, str):
        problems.append(f"runner must be a str, got {type(declaration.runner).__name__}")
    return problems


def satisfies(declaration: Any) -> bool:
    """True when `declaration` meets `IMPROVER_FENCE` (see `fence_problems`)."""
    return not fence_problems(declaration)


def fence_report(connector: Any) -> dict[str, list[str]]:
    """Why each declared runner is or is not accepted: runner -> problems (empty when accepted).

    The runner name is only the key of `fences()`; no runner is treated differently. A `fences()`
    that is not a Mapping reports nothing.
    """
    declared = capabilities(connector).fences()
    if not isinstance(declared, Mapping):
        return {}
    return {runner: fence_problems(d) for runner, d in declared.items()}


def fenced_runners(connector: Any) -> dict[str, FenceDeclaration]:
    """The connector's declarations that meet `IMPROVER_FENCE`, keyed by runner name.

    The runner name only ties a candidate (`ModelChoice.runner`) to its declaration. A
    `fences()` that is not a Mapping gives `{}`; a declaration of the wrong type is dropped.
    """
    declared = capabilities(connector).fences()
    if not isinstance(declared, Mapping):
        return {}
    return {runner: d for runner, d in declared.items() if satisfies(d)}


@runtime_checkable
class ModelConnector(Protocol):
    """A source of model candidates. Only `routes` is required; see `capabilities`.

    `run_session` runs only on the runner and model of `request.choice`. If
    `SessionResult.runner` is not empty and differs from `choice.runner`, harness2 treats the
    result as a failure.
    """

    def routes(self, role: str, *, task_id: str, substrate: str) -> list[ModelChoice]: ...


def _no_fences() -> Mapping[str, FenceDeclaration]:
    return {}


def _noop() -> None:
    return None


@dataclass(frozen=True)
class ModelCapabilities:
    """A connector's optional capabilities, with the defaults filled in (see `capabilities`)."""

    run_session: Callable[[str, SessionRequest], SessionResult] | None = None
    fences: Callable[[], Mapping[str, FenceDeclaration]] = field(default=_no_fences)
    record: Callable[[str, str, ModelChoice, Outcome], None] | None = None
    close: Callable[[], None] = _noop


def _optional_method(connector: Any, attr: str) -> Callable[..., Any] | None:
    """The method `attr` of `connector`; None when missing or None; TypeError when not callable."""
    value = getattr(connector, attr, None)
    if value is None:
        return None
    if not callable(value):
        raise TypeError(
            f"model connector capability {attr!r} must be a method or None, "
            f"got {type(value).__name__}"
        )
    return value


def capabilities(connector: Any) -> ModelCapabilities:
    """Read the optional capabilities of `connector` with getattr, filling in the defaults.

    - `run_session(role, request)` (method): default None (harness2 runs the session itself).
    - `fences()` (method): returns a mapping of runner name -> FenceDeclaration; default `{}`.
    - `record(role, task_id, choice, outcome)` (method): default None.
    - `close()` (method): default a function that does nothing.

    An attribute that is present but None counts as missing. One that is not callable raises
    TypeError. A connector with `run_session` but no `fences` raises TypeError, because the
    fence of its sessions could not be checked.
    """
    run_session = _optional_method(connector, "run_session")
    fences = _optional_method(connector, "fences")
    record = _optional_method(connector, "record")
    close = _optional_method(connector, "close")
    if run_session is not None and fences is None:
        raise TypeError(
            "model connector defines 'run_session' but not 'fences'; "
            "it must declare the fence of the runners it executes"
        )
    return ModelCapabilities(
        run_session=run_session,
        fences=fences or _no_fences,
        record=record,
        close=close or _noop,
    )


ENV_VAR = "HARNESS2_MODEL_CONNECTOR"
DEFAULT = "env"
ENTRY_POINT_GROUP = "harness2.model_connectors"

Factory = Callable[..., ModelConnector]

_REGISTRY: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    """Register a connector factory under `name`; an existing entry of that name is replaced."""
    _REGISTRY[name] = factory


def _entry_points() -> list[Any]:
    """The installed entry points of `ENTRY_POINT_GROUP`."""
    return list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def names() -> list[str]:
    """The built-in registered names plus the entry point names, sorted, without duplicates."""
    return sorted(set(_REGISTRY) | {ep.name for ep in _entry_points()})


def connector_name(*, name: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """The connector name: `name` when given, else `ENV_VAR` from `env` (or `os.environ` when
    `env` is None), else `DEFAULT`."""
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
            f"model connector {name!r} is not a valid import path; expected 'module:Attr'"
        )
    try:
        target = importlib.import_module(module_name)
    except Exception as exc:
        raise ValueError(
            f"model connector {name!r}: cannot import module {module_name!r}: {exc}"
        ) from exc
    for attr in attr_path.split("."):
        try:
            target = getattr(target, attr)
        except AttributeError as exc:
            raise ValueError(
                f"model connector {name!r}: {module_name!r} has no attribute {attr_path!r}"
            ) from exc
    if not callable(target):
        raise TypeError(f"model connector {name!r} is {type(target).__name__}, not callable")
    return target


def _load_entry_point(name: str, ep: Any) -> Factory:
    """Load an entry point as a factory; a failed load raises ValueError, a target that is not
    callable raises TypeError."""
    try:
        target = ep.load()
    except Exception as exc:
        raise ValueError(
            f"model connector {name!r}: cannot load entry point {ENTRY_POINT_GROUP}/{name}: {exc}"
        ) from exc
    if not callable(target):
        raise TypeError(
            f"model connector {name!r} entry point is {type(target).__name__}, "
            "which is not callable"
        )
    return target


def resolve_factory(name: str) -> Factory:
    """The factory for the connector `name`.

    Looked up in this order: the built-in registrations (`register`), an entry point named
    `name` in the group `ENTRY_POINT_GROUP`, and, for a name containing `:`, a `module:Attr`
    import path. A name found nowhere raises ValueError listing the registered and entry point
    names.
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
        f"unknown model connector {name!r}; registered: {registered}; "
        f"entry points ({ENTRY_POINT_GROUP}): {entry_names}"
    )


def connector_for(
    *,
    name: str | None = None,
    env: Mapping[str, str] | None = None,
    options: Mapping[str, Any] | None = None,
) -> ModelConnector:
    """Build the connector: the name from `connector_name`, the factory from `resolve_factory`,
    called as `factory(**options)`. A name found nowhere raises ValueError; a factory result
    that is not a ModelConnector raises TypeError."""
    name = connector_name(name=name, env=env)
    factory = resolve_factory(name)
    conn = factory(**dict(options or {}))
    if not isinstance(conn, ModelConnector):
        raise TypeError(
            f"model connector {name!r} factory returned {type(conn).__name__}, "
            "which is not a ModelConnector"
        )
    return conn


def check_options(factory: Callable[..., Any], options: Mapping[str, Any]) -> None:
    """Check that `factory` accepts every key in `options`.

    `inspect.signature` reads its parameters: the keys must name parameters the factory can
    take as keywords (a `**kwargs` parameter takes anything). A key it cannot take raises
    ValueError naming the keys, sorted. A factory whose signature cannot be read is not checked.
    """
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
            f"model connector factory does not accept option(s) {unknown}; "
            f"it takes {sorted(accepted)}"
        )


# Executor substrate -> the config role whose model it runs.
_EXECUTOR_ROLE_BY_SUBSTRATE = {"opencode": "executor.oc", "codex": "executor.cx"}


class EnvModelConnector:
    """The default connector: the models the environment already selects, via `config.route`.

    It has no optional capabilities, so harness2 runs every session itself.
    """

    def routes(self, role: str, *, task_id: str, substrate: str) -> list[ModelChoice]:
        if role == Role.EXECUTOR:
            config_role = _EXECUTOR_ROLE_BY_SUBSTRATE.get(substrate)
            if config_role is None:
                return []
            model = config.route(config_role).model
            return [ModelChoice(runner=substrate, model=model)] if model else []
        if role == Role.IMPROVER:
            channel = config.DEFAULT_PROPOSER_CHANNEL
            model = config.route(f"improver.{channel}").model
            return [ModelChoice(runner=channel, model=model)] if model else []
        return []


register(DEFAULT, EnvModelConnector)
