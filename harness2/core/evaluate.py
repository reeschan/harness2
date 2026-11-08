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

"""Evaluators: what reads a run graph and writes findings about it.

An evaluator has two members: `viewpoints()` returns the angles it judges (a non-empty
sequence of `Viewpoint`) and `evaluate(request)` reads one `EvalRequest` and returns a list of
`Finding`. A `Finding` carries a severity, a claim, and the `Evidence` spans of the request
graph that back it, so a reader can jump from a finding to the event that caused it.

Evaluators are found by name, like the trace sinks and the memory connectors: a built-in
registration (`register`), then an entry point of the same name in the group
`harness2.evaluators`, then, for a name containing `:`, a `module:Attr` import path. There is no
environment variable: the caller names the evaluator. A factory is called as
`factory(**options)` and must return an `Evaluator`.

Scores are kept out of the method side. An evaluator never reads a scoring file itself: scores
arrive only in `EvalRequest.scores`, and only when the run allows them (`None` otherwise).
"""

from __future__ import annotations

import importlib
import importlib.metadata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, cast, runtime_checkable

from . import trace
from .graph import Graph

ENTRY_POINT_GROUP = "harness2.evaluators"


class Severity:
    """Finding severities; `rank` orders them, heaviest first."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"
    ALL = (CRITICAL, MAJOR, MINOR, INFO)

    @staticmethod
    def rank(severity: str) -> int:
        """0 for critical up to 3 for info; an unknown name raises ValueError."""
        try:
            return Severity.ALL.index(severity)
        except ValueError:
            raise ValueError(
                f"unknown severity {severity!r}; expected one of {', '.join(Severity.ALL)}"
            ) from None


@dataclass(frozen=True)
class Viewpoint:
    """One angle of evaluation: a short name, one English sentence, and the kinds it reads."""

    name: str
    description: str
    reads: tuple[str, ...] = ()


VIEWPOINTS: tuple[Viewpoint, ...] = (
    Viewpoint(
        name="cost",
        description="What the run spent: the cost events and the models behind them.",
        reads=(
            trace.Kind.TASK,
            trace.Kind.COST,
            trace.Kind.ROLLOUT,
            trace.Kind.MODEL_ROUTE,
            trace.Kind.MODEL_SWITCH,
        ),
    ),
    Viewpoint(
        name="budget",
        description="Whether each mode's ledger adds up to the rollouts and stages it ran.",
        reads=(trace.Kind.TASK, trace.Kind.MODE, trace.Kind.ROLLOUT, trace.Kind.STAGE),
    ),
    Viewpoint(
        name="effect",
        description="Whether the change the run made improved the tree.",
        reads=(
            trace.Kind.ROLLOUT,
            trace.Kind.STAGE,
            trace.Kind.TREE_DIFF,
            trace.Kind.SELECTION,
        ),
    ),
    Viewpoint(
        name="safety",
        description="Whether the fences and the selection kept unsafe candidates out.",
        reads=(trace.Kind.MODEL_FENCE, trace.Kind.SELECTION, trace.Kind.ERROR),
    ),
    Viewpoint(
        name="reproducibility",
        description="Whether the run can be repeated: retries, model switches and errors.",
        reads=(trace.Kind.MODEL_SWITCH, trace.Kind.STAGE, trace.Kind.ERROR),
    ),
    Viewpoint(
        name="generalization",
        description="Whether what the run learned carries over to other tasks.",
        reads=(
            trace.Kind.REFLECTION,
            trace.Kind.MEMORY_RECALL,
            trace.Kind.MEMORY_STORE,
            trace.Kind.SELECTION,
        ),
    ),
)


def viewpoint(name: str) -> Viewpoint:
    """The entry of `VIEWPOINTS` named `name`; an unknown name raises ValueError."""
    for item in VIEWPOINTS:
        if item.name == name:
            return item
    known = ", ".join(item.name for item in VIEWPOINTS)
    raise ValueError(f"unknown viewpoint {name!r}; expected one of {known}")


@dataclass(frozen=True)
class Evidence:
    """A reference to one span or point event of the run graph."""

    run_id: str
    span_id: str
    note: str = ""

    def to_dict(self) -> dict:
        """The three keys as a JSON-ready dict."""
        return {"run_id": self.run_id, "span_id": self.span_id, "note": self.note}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Evidence:
        """The evidence in `d`; a missing run or span id raises ValueError."""
        try:
            run_id = d["run_id"]
            span_id = d["span_id"]
        except KeyError as exc:
            raise ValueError(f"evidence is missing {exc.args[0]!r}") from exc
        return cls(run_id=run_id, span_id=span_id, note=d.get("note", ""))


@dataclass(frozen=True)
class Finding:
    """One problem an evaluator found, with the spans that back it."""

    viewpoint: str
    severity: str
    claim: str
    evidence: tuple[Evidence, ...] = ()
    suggestion: str = ""
    key: str = ""
    evaluator: str = ""

    def __post_init__(self) -> None:
        """Check the severity and the evidence; a list of evidence becomes a tuple."""
        if self.severity not in Severity.ALL:
            raise ValueError(
                f"unknown severity {self.severity!r}; expected one of {', '.join(Severity.ALL)}"
            )
        if not isinstance(self.evidence, tuple):
            if not isinstance(self.evidence, list):
                raise TypeError(
                    f"finding evidence must be a tuple of Evidence, not "
                    f"{type(self.evidence).__name__}"
                )
            object.__setattr__(self, "evidence", tuple(self.evidence))
        for item in self.evidence:
            if not isinstance(item, Evidence):
                raise TypeError(f"finding evidence must be Evidence, not {type(item).__name__}")

    def dedupe_key(self) -> str:
        """`key` when set, else `"<viewpoint>:<claim>"`; never a hash, so it is stable."""
        return self.key or f"{self.viewpoint}:{self.claim}"

    def to_dict(self) -> dict:
        """The seven fields as JSON-ready values; `key` is written as given."""
        return {
            "viewpoint": self.viewpoint,
            "severity": self.severity,
            "claim": self.claim,
            "evidence": [item.to_dict() for item in self.evidence],
            "suggestion": self.suggestion,
            "key": self.key,
            "evaluator": self.evaluator,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Finding:
        """The finding in `d`; unknown keys (aggregated extras) are ignored."""
        evidence = d.get("evidence") or ()
        return cls(
            viewpoint=d["viewpoint"],
            severity=d["severity"],
            claim=d["claim"],
            evidence=tuple(Evidence.from_dict(item) for item in evidence),
            suggestion=d.get("suggestion", ""),
            key=d.get("key", ""),
            evaluator=d.get("evaluator", ""),
        )


@dataclass(frozen=True)
class EvalRequest:
    """What an evaluator reads. Scores arrive only here, and only when the run allows them.

    `options` holds free settings for the whole run; the `options` of a config node are passed
    to the evaluator's factory instead and are a different thing. `scores` maps a run id to the
    attrs of that run's scoring events, and is `None` unless the run allows scores.
    """

    graph: Graph
    summaries: Mapping[Path, Mapping[str, Any]]
    viewpoints: tuple[Viewpoint, ...]
    scores: Mapping[str, Sequence[Mapping[str, Any]]] | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def summary_for(self, run_id: str) -> Mapping[str, Any] | None:
        """The summary.json next to the trace of run `run_id`, or None.

        A trace file that holds more than one run has one summary; every run of that file gets
        the same summary.
        """
        node = self.graph.runs().get(run_id)
        if node is None:
            return None
        return self.summaries.get(node.source.parent)


@runtime_checkable
class Evaluator(Protocol):
    """An evaluator: the viewpoints it judges and the findings it writes for one request.

    Scores are read only from `request.scores`; an evaluator must not open a scoring file
    itself, because the run decides whether scores are allowed.
    """

    def viewpoints(self) -> Sequence[Viewpoint]:
        """The viewpoints this evaluator judges; must not be empty."""
        ...

    def evaluate(self, request: EvalRequest) -> list[Finding]:
        """The findings for `request`; evidence must point at spans of its graph."""
        ...


Factory = Callable[..., Evaluator]

_REGISTRY: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    """Register an evaluator factory under `name`; an existing entry of that name is replaced.

    The factory is called as `factory(**options)`.
    """
    _REGISTRY[name] = factory


def _entry_points(group: str = ENTRY_POINT_GROUP) -> list[Any]:
    """The installed entry points of `group`."""
    return list(importlib.metadata.entry_points(group=group))


def names() -> list[str]:
    """The built-in registered names plus the entry point names, sorted, without duplicates."""
    return sorted(set(_REGISTRY) | {ep.name for ep in _entry_points()})


def _import_factory(name: str, *, what: str) -> Callable[..., Any]:
    """Load the factory named by a `module:Attr` import path; Attr may be a dotted chain.

    A malformed path, a failed import and a missing attribute raise ValueError; an attribute
    that is not callable raises TypeError.
    """
    module_name, _, attr_path = name.partition(":")
    if name.count(":") != 1 or not module_name or not attr_path:
        raise ValueError(f"{what} {name!r} is not a valid import path; expected 'module:Attr'")
    try:
        target = importlib.import_module(module_name)
    except Exception as exc:
        raise ValueError(f"{what} {name!r}: cannot import module {module_name!r}: {exc}") from exc
    for attr in attr_path.split("."):
        try:
            target = getattr(target, attr)
        except AttributeError as exc:
            raise ValueError(
                f"{what} {name!r}: {module_name!r} has no attribute {attr_path!r}"
            ) from exc
    if not callable(target):
        raise TypeError(f"{what} {name!r} is {type(target).__name__}, which is not callable")
    return target


def _load_entry_point(name: str, ep: Any, *, group: str, what: str) -> Callable[..., Any]:
    """Load an entry point as a factory; a failed load raises ValueError, a target that is not
    callable raises TypeError."""
    try:
        target = ep.load()
    except Exception as exc:
        raise ValueError(f"{what} {name!r}: cannot load entry point {group}/{name}: {exc}") from exc
    if not callable(target):
        raise TypeError(
            f"{what} {name!r} entry point is {type(target).__name__}, which is not callable"
        )
    return target


def _resolve_in(
    name: str,
    registry: Mapping[str, Callable[..., Any]],
    *,
    group: str,
    what: str,
) -> Callable[..., Any]:
    """The factory `name` from `registry`, then an entry point of `group`, then `module:Attr`.

    `what` names the kind of factory in every error ("evaluator", "rebutter"), so the
    evaluator registry and the rebutter registry of a graph share one lookup.
    """
    factory = registry.get(name)
    if factory is not None:
        return factory
    eps = _entry_points(group)
    for ep in eps:
        if ep.name == name:
            return _load_entry_point(name, ep, group=group, what=what)
    if ":" in name:
        return _import_factory(name, what=what)
    registered = ", ".join(sorted(registry)) or "(none)"
    entry_names = ", ".join(sorted({ep.name for ep in eps})) or "(none)"
    raise ValueError(
        f"unknown {what} {name!r}; registered: {registered}; entry points ({group}): {entry_names}"
    )


def resolve_factory(name: str) -> Factory:
    """The factory for the evaluator `name`.

    Looked up in this order: the built-in registrations (`register`), an entry point named
    `name` in the group `harness2.evaluators`, and, for a name containing `:`, a `module:Attr`
    import path (see `_import_factory`). An entry point's `load()` must return a factory, the
    same as a `module:Attr` target: a callable called as `factory(**options)`. A name found
    nowhere raises ValueError listing the registered and entry point names.
    """
    return cast(
        Factory,
        _resolve_in(name, _REGISTRY, group=ENTRY_POINT_GROUP, what="evaluator"),
    )


def evaluator_for(name: str, *, options: Mapping[str, Any] | None = None) -> Evaluator:
    """Build evaluator `name` as `factory(**options)`.

    A factory result that is not an `Evaluator` raises TypeError. `options` must be a mapping
    when given; the keys are the settings of this evaluator.
    """
    if options is not None and not isinstance(options, Mapping):
        raise TypeError(
            f"evaluator {name!r} options must be a mapping, not {type(options).__name__}"
        )
    factory = resolve_factory(name)
    evaluator = factory(**dict(options or {}))
    if not isinstance(evaluator, Evaluator) or not (
        callable(evaluator.viewpoints) and callable(evaluator.evaluate)
    ):
        raise TypeError(
            f"evaluator {name!r} factory returned {type(evaluator).__name__}, "
            "which is not an Evaluator"
        )
    return evaluator


def _rules_factory(**options: Any) -> Evaluator:
    """Build the built-in `rules` evaluator without importing its module until called.

    The import stays inside the function so `harness2.evaluators.rules` may be imported first:
    a module-level import here would run while that module is only half-loaded.
    """
    from ..evaluators.rules import RulesEvaluator

    return RulesEvaluator(**options)


register("rules", _rules_factory)
