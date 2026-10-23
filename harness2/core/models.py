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
whose declared fence satisfies `FENCE_REQUIREMENTS`.

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
from .proposer import _DROP_ENV, CLAUDE_CLI_PERMISSION_MODE, CLAUDE_CLI_TOOLS


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


@dataclass(frozen=True)
class FenceDeclaration:
    """The fence a connector says it applies to a runner it executes.

    `cwd_only` means the session sees only its working directory: no directory outside it is
    added (no `--add-dir`). `restricted` is the runner's restricted mode, and `permission_mode`
    is the permission mode the session runs in.
    """

    tools: tuple[str, ...]
    cwd_only: bool
    strict_mcp: bool
    no_prompts: bool
    restricted: bool
    permission_mode: str
    dropped_env: tuple[str, ...]


@dataclass(frozen=True)
class FenceRequirement:
    """The fence harness2 requires for a runner: the tools allowed (a ceiling), and the
    switches, the permission mode and dropped environment variables that must all be present."""

    tools: tuple[str, ...]
    cwd_only: bool
    strict_mcp: bool
    no_prompts: bool
    restricted: bool
    permission_mode: str
    dropped_env: tuple[str, ...]


_SWITCHES = ("cwd_only", "strict_mcp", "no_prompts", "restricted")


def _str_collection_problem(name: str, value: Any) -> str:
    """Why `value` is not a tuple, list or frozenset of str; empty when it is."""
    if not isinstance(value, (tuple, list, frozenset)):
        return f"{name} must be a tuple, list or frozenset of str, got {type(value).__name__}"
    if not all(isinstance(item, str) for item in value):
        return f"{name} must contain only str"
    return ""


def fence_problems(declaration: Any, requirement: FenceRequirement) -> list[str]:
    """Why `declaration` does not satisfy `requirement`; an empty list means it does.

    The declaration must be a `FenceDeclaration` with well-typed fields. The declared tools must
    be a subset of the required set, each switch (`cwd_only`, `strict_mcp`, `no_prompts`,
    `restricted`) must be a bool equal to the required value, `permission_mode` must be the
    required str, and the declared dropped environment variables must include every required one.
    """
    if not isinstance(declaration, FenceDeclaration):
        return [f"declaration must be a FenceDeclaration, got {type(declaration).__name__}"]
    problems: list[str] = []
    tools_problem = _str_collection_problem("tools", declaration.tools)
    if tools_problem:
        problems.append(tools_problem)
    elif not set(declaration.tools) <= set(requirement.tools):
        extra = sorted(set(declaration.tools) - set(requirement.tools))
        problems.append(f"tools beyond the allowed set: {extra}")
    for name in _SWITCHES:
        value = getattr(declaration, name)
        wanted = getattr(requirement, name)
        if not isinstance(value, bool):
            problems.append(f"{name} must be a bool, got {type(value).__name__}")
        elif value is not wanted:
            problems.append(f"{name} must be {wanted}")
    if not isinstance(declaration.permission_mode, str):
        problems.append(
            f"permission_mode must be a str, got {type(declaration.permission_mode).__name__}"
        )
    elif declaration.permission_mode != requirement.permission_mode:
        problems.append(f"permission_mode must be {requirement.permission_mode!r}")
    env_problem = _str_collection_problem("dropped_env", declaration.dropped_env)
    if env_problem:
        problems.append(env_problem)
    elif not set(requirement.dropped_env) <= set(declaration.dropped_env):
        missing = sorted(set(requirement.dropped_env) - set(declaration.dropped_env))
        problems.append(f"dropped_env is missing: {missing}")
    return problems


def satisfies(declaration: Any, requirement: FenceRequirement) -> bool:
    """True when `declaration` is at least as strict as `requirement` (see `fence_problems`)."""
    return not fence_problems(declaration, requirement)


# Runner -> the fence harness2 requires of any connector that executes it. A runner that is not
# listed cannot be executed by a connector.
FENCE_REQUIREMENTS: Mapping[str, FenceRequirement] = {
    "claude": FenceRequirement(
        tools=tuple(CLAUDE_CLI_TOOLS.split(",")),
        cwd_only=True,
        strict_mcp=True,
        no_prompts=True,
        restricted=True,
        permission_mode=CLAUDE_CLI_PERMISSION_MODE,
        dropped_env=tuple(_DROP_ENV),
    ),
}

# Runner name -> the improver channel (`config.IMPROVER_CHANNELS`) that runs it.
RUNNER_CHANNELS: Mapping[str, str] = {"claude": "claude_cli", "opencode": "opencode"}


def _runner_problems(runner: str, declaration: Any) -> list[str]:
    """The problems of one declared runner: no requirement for it, or `fence_problems`."""
    requirement = FENCE_REQUIREMENTS.get(runner)
    if requirement is None:
        return [f"no fence requirement for {runner}"]
    return fence_problems(declaration, requirement)


def fence_report(connector: Any) -> dict[str, list[str]]:
    """Why each declared runner is or is not accepted: runner -> problems (empty when accepted).

    A runner without a requirement reports `["no fence requirement for <runner>"]`. A `fences()`
    that is not a Mapping reports nothing.
    """
    declared = capabilities(connector).fences()
    if not isinstance(declared, Mapping):
        return {}
    return {runner: _runner_problems(runner, d) for runner, d in declared.items()}


def fenced_runners(connector: Any) -> dict[str, FenceDeclaration]:
    """The connector's declared fences for the runners that have a requirement and satisfy it.

    A `fences()` that is not a Mapping gives `{}`; a declaration of the wrong type is dropped.
    """
    declared = capabilities(connector).fences()
    if not isinstance(declared, Mapping):
        return {}
    return {
        runner: declaration
        for runner, declaration in declared.items()
        if not _runner_problems(runner, declaration)
    }


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
    - `fences()` (method): returns a mapping of runner -> FenceDeclaration; default `{}`.
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
            runner = next((r for r, c in RUNNER_CHANNELS.items() if c == channel), channel)
            return [ModelChoice(runner=runner, model=model)] if model else []
        return []


register(DEFAULT, EnvModelConnector)
