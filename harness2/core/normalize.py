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

"""Normalization: turning a RawTask from a connector into a TaskSpec the loop can run.

A `Normalizer` turns one RawTask into the dict form of a TaskSpec. `normalize_task` checks
that dict with `intake.validate_task_spec`, asks the normalizer once more with the problems
as feedback when it fails, and raises `NormalizeError` when the second answer fails too.

Two normalizers are registered: `passthrough` reads an already structured task from the
RawTask without a model, and `model` asks a model through a Proposer (the improver's
channel). `normalizer_for` picks one by explicit name, then by the `HARNESS2_NORMALIZER`
environment variable, then the default `passthrough`. A name that is not registered but has
the form `module:Attr` is imported and used as the factory.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from .. import config
from . import intake
from .intake import RawTask, TaskSpec
from .proposer import Proposer
from .spec import ChannelSpec


@runtime_checkable
class Normalizer(Protocol):
    """Turns one RawTask into the dict form of a TaskSpec (see `intake.TASK_SPEC_SCHEMA`).

    `domains` lists the domains that already exist; `feedback` holds the problems of the
    previous answer, one per line ("" on the first try).
    """

    def normalize(
        self, raw: RawTask, *, domains: Sequence[str], feedback: str = ""
    ) -> Mapping[str, Any]: ...


class NormalizeError(ValueError):
    """Normalization failed twice; `problems` holds the problems of the second try."""

    def __init__(self, message: str, problems: Sequence[str]):
        super().__init__(message)
        self.problems: tuple[str, ...] = tuple(problems)


MAX_TRIES = 2


def _attempt(
    normalizer: Normalizer, raw: RawTask, domains: Sequence[str], feedback: str
) -> tuple[Any, list[str]]:
    """One normalization: (the answer, its problems). An exception is one problem."""
    try:
        obj = normalizer.normalize(raw, domains=domains, feedback=feedback)
    except Exception as exc:
        return None, [str(exc) or type(exc).__name__]
    return obj, intake.validate_task_spec(obj)


def normalize_task(normalizer: Normalizer, raw: RawTask, *, domains: Sequence[str]) -> TaskSpec:
    """Normalize `raw` into a TaskSpec, retrying once with the problems as feedback.

    The normalizer is called at most twice. An exception it raises counts as one problem
    (its message). When the second answer still has problems, NormalizeError carries them.
    """
    feedback = ""
    problems: list[str] = []
    for _ in range(MAX_TRIES):
        obj, problems = _attempt(normalizer, raw, domains, feedback)
        if not problems:
            return intake.task_spec_from_dict(obj)
        feedback = "\n".join(problems)
    raise NormalizeError(
        f"task {raw.id!r} could not be normalized:\n" + "\n".join(f"- {p}" for p in problems),
        problems,
    )


def _with_task_id(obj: Any, raw: RawTask) -> dict[str, Any]:
    if not isinstance(obj, Mapping):
        raise ValueError(f"task {raw.id!r}: the task spec must be a JSON object")
    out = dict(obj)
    out.setdefault("task_id", raw.id)
    return out


class PassthroughNormalizer:
    """Reads an already structured task: `raw.meta["task_spec"]`, else `raw.body` as JSON.

    A missing `task_id` is filled with `raw.id`. A body that is not JSON raises ValueError.
    """

    def normalize(
        self, raw: RawTask, *, domains: Sequence[str] = (), feedback: str = ""
    ) -> Mapping[str, Any]:
        spec = raw.meta.get("task_spec") if isinstance(raw.meta, Mapping) else None
        if isinstance(spec, Mapping):
            return _with_task_id(spec, raw)
        try:
            obj = json.loads(raw.body)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"task {raw.id!r}: the body is not JSON ({exc}); the passthrough normalizer "
                f"needs a structured task spec in meta['task_spec'] or the body"
            ) from exc
        return _with_task_id(obj, raw)


# Fences are recognised only at the start of a line. A JSON value cannot hold a raw
# newline inside a string, so a ``` inside a JSON string never starts a line and
# never closes the block.
_JSON_FENCE_OPEN = re.compile(r"^```json[ \t]*\r?\n", re.MULTILINE)
_FENCE_CLOSE = re.compile(r"^```[ \t]*\r?$", re.MULTILINE)


def _last_json_block(text: str) -> dict[str, Any]:
    """The JSON object in the last ```json fence of `text`.

    ValueError when there is no such fence, when the last one is not closed by a
    line holding only ```, or when its content is not a JSON object.
    """
    text = text or ""
    opens = list(_JSON_FENCE_OPEN.finditer(text))
    if not opens:
        raise ValueError("the reply has no ```json block with the task spec")
    start = opens[-1].end()
    close = _FENCE_CLOSE.search(text, start)
    if close is None:
        raise ValueError("the last ```json block is not closed with a line holding only ```")
    try:
        obj = json.loads(text[start : close.start()])
    except json.JSONDecodeError as exc:
        raise ValueError(f"the last ```json block is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("the last ```json block must hold a JSON object")
    return obj


class ModelNormalizer:
    """Asks a model, through a Proposer, to write the task spec as a ```json block."""

    def __init__(self, prop: Proposer, *, label_prefix: str = "normalize"):
        self.prop = prop
        self.label_prefix = label_prefix

    def mission(self, raw: RawTask, *, domains: Sequence[str], feedback: str = "") -> str:
        """The mission text for one normalization of `raw`."""
        domain_list = "\n".join(f"- {d}" for d in domains) or "- (none yet)"
        file_list = "\n".join(f"- {f}" for f in raw.files) or "- (none)"
        parts = [
            "# Normalize an incoming task",
            "",
            "Turn the task below into a task spec: one JSON object that matches this JSON Schema.",
            "",
            "```",
            json.dumps(intake.TASK_SPEC_SCHEMA),
            "```",
            "",
            "## Domains",
            "",
            "Existing domains:",
            domain_list,
            "",
            "Reuse an existing domain when one fits the task. Create a new domain only when "
            "none of them fits.",
            "",
            "## Rules",
            "",
            "- `prompt` is the task as the worker will read it.",
            "- `files` lists the attachment paths the worker needs, relative to the task's "
            "files directory (an empty list means every attachment).",
            "- `deliverable.what` says what to hand back; `deliverable.where` is a path "
            'relative to output/ ("" is output/ itself).',
            "- The text in `prompts` (work, domain_word, deliverable_word, yardstick) must "
            "describe the work itself. Do not use the words benchmark, evaluation, grader, "
            "rubric or score there.",
            "- Write the acceptance criteria only in `private_criteria`. Never put them in "
            "`prompt`, `prompts` or `deliverable`: the worker must not see them.",
            "",
            "## Task",
            "",
            f"id: {raw.id}",
            f"title: {raw.title}",
            "files:",
            file_list,
            "",
            "body:",
            "",
            raw.body,
            "",
        ]
        if feedback:
            parts += [
                "## Problems with the previous output",
                "",
                "Your previous output had these problems. Fix all of them:",
                "",
                feedback,
                "",
            ]
        parts += [
            "## Output",
            "",
            "End your reply with the task spec in a single ```json fenced block.",
        ]
        return "\n".join(parts)

    def normalize(
        self, raw: RawTask, *, domains: Sequence[str], feedback: str = ""
    ) -> Mapping[str, Any]:
        mission = self.mission(raw, domains=domains, feedback=feedback)
        reply = self.prop.call(mission, None, f"{self.label_prefix}.{raw.id}")
        return _last_json_block(reply.text)


ENV_VAR = "HARNESS2_NORMALIZER"
DEFAULT = "passthrough"

Factory = Callable[..., Normalizer]

_REGISTRY: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    """Register a normalizer factory under `name`; an existing entry of that name is replaced.

    The factory is called as `factory(proposer=proposer)` when `normalizer_for` gets a
    proposer, and as `factory()` otherwise.
    """
    _REGISTRY[name] = factory


def names() -> list[str]:
    """The registered normalizer names, sorted."""
    return sorted(_REGISTRY)


def normalizer_name(*, name: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """The normalizer name: `name` when given, else `ENV_VAR` from `env` (or `os.environ`
    when `env` is None), else `DEFAULT`.
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
        raise ValueError(f"normalizer {name!r} is not a valid import path; expected 'module:Attr'")
    try:
        target = importlib.import_module(module_name)
    except Exception as exc:
        raise ValueError(
            f"normalizer {name!r}: cannot import module {module_name!r}: {exc}"
        ) from exc
    for attr in attr_path.split("."):
        try:
            target = getattr(target, attr)
        except AttributeError as exc:
            raise ValueError(
                f"normalizer {name!r}: {module_name!r} has no attribute {attr_path!r}"
            ) from exc
    if not callable(target):
        raise TypeError(f"normalizer {name!r} is {type(target).__name__}, which is not callable")
    return target


def normalizer_for(
    *,
    name: str | None = None,
    env: Mapping[str, str] | None = None,
    proposer: Proposer | None = None,
) -> Normalizer:
    """Build a normalizer.

    The name comes from `normalizer_name`. A registered name is used first; otherwise a name
    containing `:` is loaded as a `module:Attr` import path. Any other unregistered name
    raises ValueError; a factory result that is not a Normalizer raises TypeError.
    """
    name = normalizer_name(name=name, env=env)
    factory = _REGISTRY.get(name)
    if factory is None:
        if ":" not in name:
            raise ValueError(
                f"unknown normalizer {name!r}; registered: {', '.join(names()) or '(none)'}"
            )
        factory = _import_factory(name)
    norm = factory(proposer=proposer) if proposer is not None else factory()
    if not isinstance(norm, Normalizer):
        raise TypeError(
            f"normalizer {name!r} factory returned {type(norm).__name__}, which is not a Normalizer"
        )
    return norm


def _passthrough_factory(**_options: Any) -> Normalizer:
    return PassthroughNormalizer()


def _model_factory(*, proposer: Proposer | None = None) -> Normalizer:
    """A ModelNormalizer on `proposer`, else on a Proposer for the `normalizer` route."""
    if proposer is None:
        model = config.route("normalizer").model
        if not model.strip():
            raise ValueError(
                "the 'normalizer' route has no model configured, so the 'model' normalizer "
                "cannot start; give config.ROLES['normalizer'] a model, pass proposer= to "
                f"normalizer_for, or set {ENV_VAR}=passthrough to use structured tasks as-is"
            )
        proposer = Proposer(ChannelSpec(config.DEFAULT_PROPOSER_CHANNEL, model))
    return ModelNormalizer(proposer)


register(DEFAULT, _passthrough_factory)
register("model", _model_factory)
