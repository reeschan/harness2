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

"""The evaluation graph: evaluator, rebutter and aggregate nodes run as one DAG.

A config is a JSON dict with three keys:

    {
      "nodes": [
        {"id": "rules", "type": "evaluator", "evaluator": "rules",
         "viewpoints": ["cost", "budget"], "options": {}},
        {"id": "rebut", "type": "rebut", "rebutter": "module:Attr"},
        {"id": "aggregate", "type": "aggregate"}
      ],
      "edges": [["rules", "rebut"], ["rebut", "aggregate"]],
      "allow_scores": false
    }

A node's `id` is unique, its `type` is `evaluator`, `rebut` or `aggregate`; the nodes and the
edges must form a DAG with exactly one aggregate node, which is the sink every node reaches.
`default_config` names that node `aggregate`, or `aggregate_1`, `aggregate_2`, ... when an
evaluator already has that name.
An evaluator node runs its evaluator (built with `evaluate.evaluator_for`) and keeps the
findings of the node's `viewpoints` (or of every viewpoint the evaluator declares); a rebut
node passes the findings it receives to its rebutter and keeps the verdicts' stances; the
aggregate node merges what arrives. An unknown name is a config error (`ValueError`), while an
exception thrown after a name resolves goes to `EvalResult.errors` and the run keeps going.

Scores reach an evaluator only when the config says `allow_scores: true`; otherwise the request
passed on has `scores=None` and the graph is cut down to the nodes that do not start with
`trace.JUDGE_PREFIX`, so the same rule covers the evidence check.

Aggregation drops a finding without evidence in the graph (`dropped["no_evidence"]`), drops a
finding whose dedupe key a rebutter denied (`dropped["denied"]`), and merges findings with the
same `dedupe_key()`. The merged result is an `AggregatedFinding`, whose JSON has the seven keys
of `evaluate.Finding.to_dict` plus `viewpoints` and `evaluators`:

    {"viewpoint": "cost", "viewpoints": ["cost", "budget"], "severity": "major",
     "claim": "...", "evidence": [{"run_id": "...", "span_id": "...", "note": ""}],
     "suggestion": "...", "key": "...", "evaluator": "...", "evaluators": ["a", "b"]}

`viewpoint` is the first viewpoint and `viewpoints` holds all of them; the findings are sorted
by `Severity.rank`, then by the number of viewpoints (more first), then by `key`.
"""

from __future__ import annotations

import graphlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from . import evaluate, trace
from .evaluate import EvalRequest, Evidence, Finding, Severity, Viewpoint

REBUTTER_GROUP = "harness2.rebutters"
NODE_TYPES = ("evaluator", "rebut", "aggregate")
STANCES = ("support", "deny", "hold")

_AGGREGATE_ID = "aggregate"
_VIEWPOINT_NAME = re.compile(r"[a-z][a-z0-9_]*")
_MAX_ERROR_CHARS = 300


def load_config(path: Path | str) -> dict:
    """Read a JSON graph config and check it; a bad file or a bad config raises ValueError."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read graph config {source}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"graph config {source} is not valid JSON: {exc}") from exc
    check_config(data)
    return data


def default_config(evaluators: Sequence[str], *, allow_scores: bool = False) -> dict:
    """One evaluator node per name (id = name), all wired to one aggregate node.

    The aggregate id is `aggregate`, or `aggregate_1`, `aggregate_2`, ... when one of the
    evaluators already uses that name.
    """
    aggregate_id = _aggregate_id(evaluators)
    nodes = [{"id": name, "type": "evaluator", "evaluator": name} for name in evaluators]
    nodes.append({"id": aggregate_id, "type": "aggregate"})
    return {
        "nodes": nodes,
        "edges": [[name, aggregate_id] for name in evaluators],
        "allow_scores": allow_scores,
    }


def _aggregate_id(evaluators: Sequence[str]) -> str:
    """An aggregate node id that none of `evaluators` uses."""
    taken = set(evaluators)
    candidate = _AGGREGATE_ID
    index = 0
    while candidate in taken:
        index += 1
        candidate = f"{_AGGREGATE_ID}_{index}"
    return candidate


def check_config(
    config: Mapping[str, Any], *, viewpoints: Sequence[Viewpoint] = evaluate.VIEWPOINTS
) -> None:
    """Raise ValueError naming the first problem with a graph config.

    The config is a mapping with only `nodes`, `edges` and `allow_scores`. A node has a unique
    non-empty `id`, a known `type`, the name its type needs (`evaluator` or `rebutter`), and
    optionally `viewpoints` (named in `viewpoints`) and `options` (a mapping). Edges join known
    ids without self loops or repeats, the graph is a DAG, and exactly one aggregate node is
    the sink every node reaches. The evaluator and rebutter names must also resolve through
    `evaluate.resolve_factory` / `resolve_rebutter_factory`: an unknown name is a config error,
    not a finding, so this raises instead of leaving it to `run`.
    """
    if not isinstance(config, Mapping):
        raise ValueError(f"graph config must be a mapping, not {type(config).__name__}")
    extra = sorted(set(config) - {"nodes", "edges", "allow_scores"})
    if extra:
        raise ValueError(f"graph config has unknown keys: {', '.join(extra)}")
    nodes = config.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("graph config 'nodes' must be a list")
    known_viewpoints = {item.name for item in viewpoints}
    kinds: dict[str, str] = {}
    names: dict[str, str] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            raise ValueError(f"graph config node {index} must be a mapping")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError(f"graph config node {index} needs a non-empty string 'id'")
        if node_id in kinds:
            raise ValueError(f"graph config node id {node_id!r} is used twice")
        where = f"node {node_id!r}"
        node_type = node.get("type")
        if node_type not in NODE_TYPES:
            raise ValueError(
                f"graph config {where} has unknown type {node_type!r}; "
                f"expected one of {', '.join(NODE_TYPES)}"
            )
        allowed = {"id", "type", "options"}
        if node_type == "evaluator":
            allowed |= {"evaluator", "viewpoints"}
            name = node.get("evaluator")
            if not isinstance(name, str) or not name:
                raise ValueError(f"graph config {where} needs a non-empty string 'evaluator'")
            names[node_id] = name
        elif node_type == "rebut":
            allowed |= {"rebutter", "viewpoints"}
            name = node.get("rebutter")
            if not isinstance(name, str) or not name:
                raise ValueError(f"graph config {where} needs a non-empty string 'rebutter'")
            names[node_id] = name
        unknown_node_keys = sorted(set(node) - allowed)
        if unknown_node_keys:
            raise ValueError(
                f"graph config {where} has unknown keys: {', '.join(unknown_node_keys)}"
            )
        if "viewpoints" in node:
            _check_viewpoint_names(node["viewpoints"], known_viewpoints, where)
        if "options" in node and not isinstance(node["options"], Mapping):
            raise ValueError(
                f"graph config {where} 'options' must be a mapping, "
                f"not {type(node['options']).__name__}"
            )
        kinds[node_id] = node_type

    edges = config.get("edges", [])
    if not isinstance(edges, list):
        raise ValueError("graph config 'edges' must be a list")
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, edge in enumerate(edges):
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            raise ValueError(f"graph config edge {index} must be a pair [src, dst]")
        src, dst = edge
        if not isinstance(src, str) or not isinstance(dst, str):
            raise ValueError(f"graph config edge {index} must name two node ids as strings")
        if src not in kinds:
            raise ValueError(f"graph config edge {index} names unknown node {src!r}")
        if dst not in kinds:
            raise ValueError(f"graph config edge {index} names unknown node {dst!r}")
        if src == dst:
            raise ValueError(f"graph config edge {index} loops on node {src!r}")
        if (src, dst) in seen:
            raise ValueError(f"graph config edge {index} repeats {src!r} -> {dst!r}")
        seen.add((src, dst))
        pairs.append((src, dst))

    allow_scores = config.get("allow_scores", False)
    if not isinstance(allow_scores, bool):
        raise ValueError(
            f"graph config 'allow_scores' must be true or false, not {type(allow_scores).__name__}"
        )

    predecessors: dict[str, set[str]] = {node_id: set() for node_id in kinds}
    successors: dict[str, list[str]] = {node_id: [] for node_id in kinds}
    for src, dst in pairs:
        predecessors[dst].add(src)
        successors[src].append(dst)
    try:
        graphlib.TopologicalSorter(predecessors).prepare()
    except graphlib.CycleError as exc:
        raise ValueError(f"graph config is not a DAG: {exc}") from None

    aggregates = [node_id for node_id, kind in kinds.items() if kind == "aggregate"]
    if len(aggregates) != 1:
        raise ValueError(f"graph config needs exactly one aggregate node, found {len(aggregates)}")
    aggregate_id = aggregates[0]
    for node_id, kind in kinds.items():
        if kind == "evaluator" and predecessors[node_id]:
            raise ValueError(
                f"graph config evaluator node {node_id!r} must not have incoming edges"
            )
        if kind == "rebut" and not predecessors[node_id]:
            raise ValueError(f"graph config rebut node {node_id!r} needs an incoming edge")
        if kind == "aggregate" and successors[node_id]:
            raise ValueError(
                f"graph config aggregate node {node_id!r} must not have outgoing edges"
            )
    reachable = {aggregate_id}
    changed = True
    while changed:
        changed = False
        for node_id, targets in successors.items():
            if node_id in reachable:
                continue
            if any(target in reachable for target in targets):
                reachable.add(node_id)
                changed = True
    for node_id in kinds:
        if node_id not in reachable:
            raise ValueError(
                f"graph config node {node_id!r} does not reach the aggregate node {aggregate_id!r}"
            )

    for node_id, kind in kinds.items():
        if kind == "evaluator":
            evaluate.resolve_factory(names[node_id])
        elif kind == "rebut":
            resolve_rebutter_factory(names[node_id])


def _check_viewpoint_names(values: Any, known: set[str], where: str) -> None:
    """Raise ValueError unless `values` is a non-empty list of viewpoint names in `known`."""
    if not isinstance(values, list) or not values:
        raise ValueError(f"graph config {where} 'viewpoints' must be a non-empty list of names")
    for value in values:
        if not isinstance(value, str) or not _VIEWPOINT_NAME.fullmatch(value):
            raise ValueError(f"graph config {where} viewpoint {value!r} must match [a-z][a-z0-9_]*")
        if value not in known:
            raise ValueError(
                f"graph config {where} has unknown viewpoint {value!r}; "
                f"expected one of {', '.join(sorted(known))}"
            )


@dataclass(frozen=True)
class Verdict:
    """A rebutter's stance on the finding with dedupe key `key`."""

    key: str
    stance: str

    def __post_init__(self) -> None:
        """Check the stance; an unknown name raises ValueError."""
        if self.stance not in STANCES:
            raise ValueError(
                f"unknown stance {self.stance!r}; expected one of {', '.join(STANCES)}"
            )


@runtime_checkable
class Rebutter(Protocol):
    """A rebutter: the verdicts it has on the findings it receives.

    The key of a verdict is the `dedupe_key()` of one of the incoming findings; a key that no
    incoming finding carries is ignored by the rebut node.
    """

    def rebut(self, findings: Sequence[Finding], request: EvalRequest) -> list[Verdict]:
        """The verdicts on `findings`; the request is the one the evaluators saw."""
        ...


RebutterFactory = Callable[..., Rebutter]

_REBUTTERS: dict[str, RebutterFactory] = {}


def register_rebutter(name: str, factory: RebutterFactory) -> None:
    """Register a rebutter factory under `name`; an existing entry of that name is replaced.

    The factory is called as `factory(**options)`.
    """
    _REBUTTERS[name] = factory


def rebutter_names() -> list[str]:
    """The registered names plus the entry point names, sorted, without duplicates."""
    entry_names = {ep.name for ep in evaluate._entry_points(REBUTTER_GROUP)}
    return sorted(set(_REBUTTERS) | entry_names)


def resolve_rebutter_factory(name: str) -> RebutterFactory:
    """The factory for the rebutter `name`, looked up like an evaluator of the same rules.

    The built-in registrations win, then an entry point of `REBUTTER_GROUP`, then a
    `module:Attr` import path. A name found nowhere raises ValueError.
    """
    return evaluate._resolve_in(name, _REBUTTERS, group=REBUTTER_GROUP, what="rebutter")


def rebutter_for(name: str, *, options: Mapping[str, Any] | None = None) -> Rebutter:
    """Build rebutter `name` as `factory(**options)`; a result without `rebut` is a TypeError."""
    if options is not None and not isinstance(options, Mapping):
        raise TypeError(
            f"rebutter {name!r} options must be a mapping, not {type(options).__name__}"
        )
    factory = resolve_rebutter_factory(name)
    rebutter = factory(**dict(options or {}))
    if not isinstance(rebutter, Rebutter) or not callable(rebutter.rebut):
        raise TypeError(
            f"rebutter {name!r} factory returned {type(rebutter).__name__}, which is not a Rebutter"
        )
    return rebutter


@dataclass(frozen=True)
class AggregatedFinding(Finding):
    """A finding merged from one or more findings with the same dedupe key.

    `viewpoint` is the first viewpoint and `viewpoints` holds all of them; `evaluators` holds
    the evaluator names that wrote them, in first-seen order. `key` is the dedupe key.
    """

    viewpoints: tuple[str, ...] = ()
    evaluators: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """`Finding.to_dict` plus `viewpoints` and `evaluators`, both as lists."""
        data = super().to_dict()
        data["viewpoints"] = list(self.viewpoints)
        data["evaluators"] = list(self.evaluators)
        return data

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> AggregatedFinding:
        """Read back `to_dict`; a missing `viewpoints` becomes `(viewpoint,)`."""
        base = Finding.from_dict(d)
        viewpoints = d.get("viewpoints")
        evaluators = d.get("evaluators")
        return cls(
            viewpoint=base.viewpoint,
            severity=base.severity,
            claim=base.claim,
            evidence=base.evidence,
            suggestion=base.suggestion,
            key=base.key,
            evaluator=base.evaluator,
            viewpoints=tuple(viewpoints) if viewpoints else (base.viewpoint,),
            evaluators=tuple(evaluators or ()),
        )


@dataclass
class EvalResult:
    """What one run of the evaluation graph produced."""

    findings: list[AggregatedFinding] = field(default_factory=list)
    by_viewpoint: dict[str, list[Finding]] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=lambda: {"no_evidence": 0, "denied": 0})
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """`findings` (each through `to_dict`), `dropped` and `errors`, JSON-ready."""
        return {
            "findings": [finding.to_dict() for finding in self.findings],
            "dropped": dict(self.dropped),
            "errors": list(self.errors),
        }


def run(config: Mapping[str, Any], request: EvalRequest) -> EvalResult:
    """Check the config, resolve every name, then run the nodes in topological order.

    A config error (a bad shape, an unknown evaluator or rebutter name) raises ValueError or
    TypeError before any node runs. After that, a failing factory, `viewpoints()`, `evaluate()`
    or `rebut()` only adds a line to `errors`; the node's output is empty (a rebut node passes
    the findings through unchanged).
    """
    check_config(config, viewpoints=request.viewpoints)
    result = EvalResult()
    nodes = list(config["nodes"])
    by_id = {node["id"]: node for node in nodes}
    position = {node["id"]: index for index, node in enumerate(nodes)}
    predecessors: dict[str, set[str]] = {node["id"]: set() for node in nodes}
    for src, dst in config.get("edges", []):
        predecessors[dst].add(src)
    order = list(graphlib.TopologicalSorter(predecessors).static_order())

    node_request = _request_for_run(config, request)
    batches: dict[str, _Batch] = {}
    findings: list[AggregatedFinding] = []
    for node_id in order:
        node = by_id[node_id]
        incoming = _combine(
            batches[src] for src in sorted(predecessors[node_id], key=position.__getitem__)
        )
        if node["type"] == "evaluator":
            batches[node_id] = _run_evaluator(node, node_request, result)
        elif node["type"] == "rebut":
            batches[node_id] = _run_rebut(node, incoming, node_request, result)
        else:
            findings = _aggregate(incoming, node_request, result)
            batches[node_id] = _Batch()
    result.findings = findings
    return result


def _request_for_run(config: Mapping[str, Any], request: EvalRequest) -> EvalRequest:
    """The request the evaluators and rebutters see: scores and judge nodes only when allowed."""
    if config.get("allow_scores", False):
        return request
    kinds = {
        node.kind
        for node in request.graph.nodes.values()
        if not node.kind.startswith(trace.JUDGE_PREFIX)
    }
    return replace(request, graph=request.graph.subgraph(kinds=kinds), scores=None)


@dataclass(frozen=True)
class _Item:
    """One finding with where it came from: (evaluator node id, index in its output)."""

    origin: tuple[str, int]
    finding: Finding


@dataclass
class _Batch:
    """What flows along an edge: the findings and the stances rebut nodes added."""

    items: list[_Item] = field(default_factory=list)
    stances: dict[str, set[str]] = field(default_factory=dict)


def _combine(batches: Iterable[_Batch]) -> _Batch:
    """One batch with every finding of `batches` and the union of their stances.

    An item reaches this point along one path per incoming edge, so an `origin` already added
    is skipped: a diamond of rebut nodes must not let the same finding through more than once.
    """
    combined = _Batch()
    seen: set[tuple[str, int]] = set()
    for batch in batches:
        for item in batch.items:
            if item.origin in seen:
                continue
            seen.add(item.origin)
            combined.items.append(item)
        for key, values in batch.stances.items():
            combined.stances.setdefault(key, set()).update(values)
    return combined


def _run_evaluator(node: Mapping[str, Any], request: EvalRequest, result: EvalResult) -> _Batch:
    """Run one evaluator node and keep the findings of the viewpoints it chose."""
    node_id = node["id"]
    name = node["evaluator"]
    batch = _Batch()
    try:
        evaluator = evaluate.evaluator_for(name, options=node.get("options") or {})
    except Exception as exc:
        result.errors.append(_error("evaluator", node_id, name, exc))
        return batch
    try:
        declared = list(evaluator.viewpoints())
    except Exception as exc:
        result.errors.append(_error("evaluator", node_id, name, exc))
        return batch
    declared_names: list[str] = []
    for item in declared:
        if not isinstance(item, Viewpoint):
            result.errors.append(
                _message(
                    "evaluator",
                    node_id,
                    name,
                    f"viewpoints() returned {type(item).__name__}, not a Viewpoint",
                )
            )
            return batch
        declared_names.append(item.name)
    by_name = {item.name: item for item in request.viewpoints}
    chosen_names = _chosen_viewpoints(node, declared_names, by_name, result, node_id, name)
    chosen = tuple(by_name[viewpoint_name] for viewpoint_name in chosen_names)
    for viewpoint_name in chosen_names:
        result.by_viewpoint.setdefault(viewpoint_name, [])
    try:
        findings = evaluator.evaluate(replace(request, viewpoints=chosen))
    except Exception as exc:
        result.errors.append(_error("evaluator", node_id, name, exc))
        return batch
    if not isinstance(findings, list):
        result.errors.append(
            _message(
                "evaluator",
                node_id,
                name,
                f"evaluate() returned {type(findings).__name__}, not a list",
            )
        )
        return batch
    selected = set(chosen_names)
    for index, finding in enumerate(findings):
        if not isinstance(finding, Finding):
            result.errors.append(
                _message(
                    "evaluator",
                    node_id,
                    name,
                    f"evaluate() returned {type(finding).__name__}, not a Finding",
                )
            )
            continue
        bad_evidence = next(
            (
                reference
                for reference in finding.evidence
                if not isinstance(reference.run_id, str) or not isinstance(reference.span_id, str)
            ),
            None,
        )
        if bad_evidence is not None:
            result.errors.append(
                _message(
                    "evaluator",
                    node_id,
                    name,
                    "finding evidence run_id and span_id must be str, "
                    f"not {type(bad_evidence.run_id).__name__} and "
                    f"{type(bad_evidence.span_id).__name__}",
                )
            )
            continue
        if finding.viewpoint not in selected:
            if finding.viewpoint not in by_name:
                result.errors.append(
                    _message(
                        "evaluator",
                        node_id,
                        name,
                        f"finding viewpoint {finding.viewpoint!r} is not a viewpoint of this run",
                    )
                )
            continue
        if not finding.evaluator:
            finding = replace(finding, evaluator=name)
        result.by_viewpoint[finding.viewpoint].append(finding)
        batch.items.append(_Item(origin=(node_id, index), finding=finding))
    return batch


def _chosen_viewpoints(
    node: Mapping[str, Any],
    declared: Sequence[str],
    by_name: Mapping[str, Viewpoint],
    result: EvalResult,
    node_id: str,
    name: str,
) -> list[str]:
    """The viewpoint names an evaluator node judges, in the order it declares them.

    A node without `viewpoints` judges every viewpoint it declares that the run has; a node
    with `viewpoints` judges only those, and names it does not declare are an error.
    """
    wanted = node.get("viewpoints")
    chosen: list[str] = []
    if not wanted:
        for viewpoint_name in declared:
            if viewpoint_name not in by_name:
                result.errors.append(
                    _message(
                        "evaluator",
                        node_id,
                        name,
                        f"viewpoint {viewpoint_name!r} is not a viewpoint of this run",
                    )
                )
                continue
            if viewpoint_name not in chosen:
                chosen.append(viewpoint_name)
        return chosen
    declared_set = set(declared)
    for viewpoint_name in wanted:
        if viewpoint_name not in declared_set:
            result.errors.append(
                _message(
                    "evaluator",
                    node_id,
                    name,
                    f"viewpoint {viewpoint_name!r} is not declared by the evaluator",
                )
            )
            continue
        if viewpoint_name not in chosen:
            chosen.append(viewpoint_name)
    return chosen


def _run_rebut(
    node: Mapping[str, Any],
    batch: _Batch,
    request: EvalRequest,
    result: EvalResult,
) -> _Batch:
    """Run one rebut node: add its verdicts' stances and pass every finding through."""
    if not batch.items:
        return batch
    node_id = node["id"]
    name = node["rebutter"]
    try:
        rebutter = rebutter_for(name, options=node.get("options") or {})
    except Exception as exc:
        result.errors.append(_error("rebut", node_id, name, exc))
        return batch
    findings = [item.finding for item in batch.items]
    try:
        verdicts = rebutter.rebut(findings, request)
    except Exception as exc:
        result.errors.append(_error("rebut", node_id, name, exc))
        return batch
    if not isinstance(verdicts, list):
        result.errors.append(
            _message(
                "rebut",
                node_id,
                name,
                f"rebut() returned {type(verdicts).__name__}, not a list",
            )
        )
        return batch
    keys = {finding.dedupe_key() for finding in findings}
    stances = {key: set(values) for key, values in batch.stances.items()}
    for verdict in verdicts:
        if not isinstance(verdict, Verdict):
            result.errors.append(
                _message(
                    "rebut",
                    node_id,
                    name,
                    f"rebut() returned {type(verdict).__name__}, not a Verdict",
                )
            )
            continue
        if verdict.key not in keys:
            result.errors.append(
                _message(
                    "rebut",
                    node_id,
                    name,
                    f"verdict key {verdict.key!r} is not a finding of this node",
                )
            )
            continue
        stances.setdefault(verdict.key, set()).add(verdict.stance)
    return _Batch(items=list(batch.items), stances=stances)


def _aggregate(batch: _Batch, request: EvalRequest, result: EvalResult) -> list[AggregatedFinding]:
    """Drop, deny, merge and order the findings that reached the aggregate node."""
    unique: list[_Item] = []
    seen_origins: set[tuple[str, int]] = set()
    for item in batch.items:
        if item.origin in seen_origins:
            continue
        seen_origins.add(item.origin)
        unique.append(item)
    surviving: list[Finding] = []
    for item in unique:
        finding = item.finding
        evidence = tuple(
            reference
            for reference in finding.evidence
            if request.graph.find(reference.run_id, reference.span_id) is not None
        )
        if not evidence:
            result.dropped["no_evidence"] += 1
            continue
        if "deny" in batch.stances.get(finding.dedupe_key(), ()):
            result.dropped["denied"] += 1
            continue
        if len(evidence) != len(finding.evidence):
            finding = replace(finding, evidence=evidence)
        surviving.append(finding)
    groups: dict[str, list[Finding]] = {}
    keys: list[str] = []
    for finding in surviving:
        key = finding.dedupe_key()
        if key not in groups:
            groups[key] = []
            keys.append(key)
        groups[key].append(finding)
    merged = [_merge(groups[key]) for key in keys]
    merged.sort(
        key=lambda finding: (
            Severity.rank(finding.severity),
            -len(finding.viewpoints),
            finding.key,
        )
    )
    return merged


def _merge(findings: Sequence[Finding]) -> AggregatedFinding:
    """One finding from findings with the same dedupe key.

    `claim` and `suggestion` come from the first finding; the viewpoints, the evidence and the
    evaluator names are the union in first-seen order; the severity is the heaviest.
    """
    first = findings[0]
    viewpoints: list[str] = []
    evaluators: list[str] = []
    evidence: list[Evidence] = []
    seen_evidence: set[tuple[str, str]] = set()
    for finding in findings:
        if finding.viewpoint not in viewpoints:
            viewpoints.append(finding.viewpoint)
        if finding.evaluator and finding.evaluator not in evaluators:
            evaluators.append(finding.evaluator)
        for reference in finding.evidence:
            key = (reference.run_id, reference.span_id)
            if key in seen_evidence:
                continue
            seen_evidence.add(key)
            evidence.append(reference)
    heaviest = min(findings, key=lambda finding: Severity.rank(finding.severity))
    return AggregatedFinding(
        viewpoint=first.viewpoint,
        severity=heaviest.severity,
        claim=first.claim,
        evidence=tuple(evidence),
        suggestion=first.suggestion,
        key=first.dedupe_key(),
        evaluator=first.evaluator,
        viewpoints=tuple(viewpoints),
        evaluators=tuple(evaluators),
    )


def _error(kind: str, node_id: str, name: str, exc: BaseException) -> str:
    """One error line for an exception a node threw; the message is cut to 300 characters."""
    text = str(exc)
    if len(text) > _MAX_ERROR_CHARS:
        text = text[:_MAX_ERROR_CHARS]
    return f"{kind} node {node_id!r} ({name}): {type(exc).__name__}: {text}"


def _message(kind: str, node_id: str, name: str, text: str) -> str:
    """One error line for a result that has the wrong shape."""
    return f"{kind} node {node_id!r} ({name}): {text}"
