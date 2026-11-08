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

"""The graph of one or more task runs: what happened and what contained what.

`build(paths)` reads `trace.jsonl` files with `harness2.core.trace_file.read` and turns
them into a `Graph`. A span is two events sharing a `span_id` (a start and an end) and
becomes one `Node`; a point event is a node of its own. The node's `attrs` combine the
start and end attrs, with the end winning, and drop `phase`; `status` and `dur_s` come
from the end event, so a point event has neither. `parent_id` becomes an `Edge` from the
parent node to the child, with `kind="contains"`.

Nothing here knows the kinds of harness2's work beyond the `task` root: `roots()` and
`runs()` find `trace.Kind.TASK`, and every other query takes the kinds the caller asks
for. Kinds starting with `trace.JUDGE_PREFIX` are left out unless `include_judge=True`,
so the method side is the default view.

A node whose `parent_id` names a span that is not in the graph (a missing event, or a
judge parent left out) stays a root and its key goes into `warnings`; reading never
fails. `subgraph(kinds=..., run_ids=...)` cuts the graph down and relinks every kept
node to its nearest kept ancestor, so the result is still a forest of `contains` edges.
`load_runs(root)` builds the graph of every `trace.jsonl` under `root` and reads the
`summary.json` of each task run directory next to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from . import trace, trace_file

CONTAINS = "contains"
TRACE_NAME = trace_file.FILE_NAME
SUMMARY_NAME = "summary.json"


@dataclass
class Node:
    """One span or point event: its identity, what it is, when it ran and its attrs."""

    key: tuple[str, str]
    kind: str
    run_id: str
    span_id: str
    parent: tuple[str, str] | None
    attrs: dict[str, Any]
    start_ts: str | None
    end_ts: str | None
    status: str | None
    dur_s: float | None
    source: Path


@dataclass(frozen=True)
class Edge:
    """A containment edge: `src` contained `dst` (`kind="contains"`)."""

    src: tuple[str, str]
    dst: tuple[str, str]
    kind: str = CONTAINS


@dataclass
class Graph:
    """Nodes by `(run_id, span_id)`, the containment edges and the read warnings."""

    nodes: dict[tuple[str, str], Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _parents: dict[tuple[str, str], tuple[str, str]] | None = field(
        default=None, repr=False, compare=False
    )
    _children: dict[tuple[str, str], set[tuple[str, str]]] | None = field(
        default=None, repr=False, compare=False
    )

    def roots(self) -> list[Node]:
        """The `task` nodes, sorted by key."""
        return [node for node in self._sorted_nodes() if node.kind == trace.Kind.TASK]

    def children(self, key: tuple[str, str]) -> list[Node]:
        """The nodes this node contained directly, sorted by key."""
        _, children = self._index()
        targets = children.get(key)
        if not targets:
            return []
        return [self.nodes[target] for target in sorted(targets) if target in self.nodes]

    def parent(self, key: tuple[str, str]) -> Node | None:
        """The node that contained this one, or None when it is a root."""
        parents, _ = self._index()
        source = parents.get(key)
        if source is None:
            return None
        return self.nodes.get(source)

    def ancestors(self, key: tuple[str, str]) -> list[Node]:
        """The parents of this node from the nearest to the farthest, without itself."""
        chain: list[Node] = []
        seen = {key}
        current = self.parent(key)
        while current is not None and current.key not in seen:
            chain.append(current)
            seen.add(current.key)
            current = self.parent(current.key)
        return chain

    def of_kind(self, *kinds: str) -> list[Node]:
        """The nodes of any of these kinds, sorted by key."""
        wanted = set(kinds)
        return [node for node in self._sorted_nodes() if node.kind in wanted]

    def runs(self) -> dict[str, Node]:
        """The `task` node of each run, by run id."""
        found: dict[str, Node] = {}
        for node in self._sorted_nodes():
            if node.kind == trace.Kind.TASK:
                found.setdefault(node.run_id, node)
        return found

    def subgraph(
        self,
        kinds: Iterable[str] | None = None,
        run_ids: Iterable[str] | None = None,
    ) -> Graph:
        """The nodes matching `kinds` and `run_ids`, relinked to their nearest kept ancestor.

        A node with no kept ancestor stays a root, so the edges of the result only join kept
        nodes. The nodes are copies: changing a copy's `parent` does not change `self`.
        """
        wanted_kinds = None if kinds is None else set(kinds)
        wanted_runs = None if run_ids is None else set(run_ids)
        kept = {
            key: node
            for key, node in self.nodes.items()
            if (wanted_kinds is None or node.kind in wanted_kinds)
            and (wanted_runs is None or node.run_id in wanted_runs)
        }
        result = Graph(warnings=list(self.warnings))
        for key in sorted(kept):
            node = kept[key]
            ancestor = self._nearest_kept_ancestor(key, kept)
            parent = None if ancestor is None else ancestor.key
            if parent is not None:
                result.edges.append(Edge(parent, key))
            result.nodes[key] = replace(node, parent=parent, attrs=dict(node.attrs))
        result._reindex()
        return result

    def find(self, run_id: str, span_id: str) -> Node | None:
        """The node with this run id and span id, or None."""
        return self.nodes.get((run_id, span_id))

    def _sorted_nodes(self) -> list[Node]:
        """Every node, sorted by key."""
        return [self.nodes[key] for key in sorted(self.nodes)]

    def _index(
        self,
    ) -> tuple[
        dict[tuple[str, str], tuple[str, str]],
        dict[tuple[str, str], set[tuple[str, str]]],
    ]:
        """The child → parent and parent → children indexes, built once from `edges`."""
        if self._parents is None or self._children is None:
            self._reindex()
        assert self._parents is not None and self._children is not None
        return self._parents, self._children

    def _reindex(self) -> None:
        """Rebuild the indexes from `edges`; the first edge to a child names its parent."""
        parents: dict[tuple[str, str], tuple[str, str]] = {}
        children: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for edge in self.edges:
            parents.setdefault(edge.dst, edge.src)
            children.setdefault(edge.src, set()).add(edge.dst)
        self._parents = parents
        self._children = children

    def _nearest_kept_ancestor(
        self, key: tuple[str, str], kept: dict[tuple[str, str], Node]
    ) -> Node | None:
        """The closest ancestor of `key` that is in `kept`, or None."""
        for ancestor in self.ancestors(key):
            if ancestor.key in kept:
                return ancestor
        return None


def build(paths: Iterable[Path], *, include_judge: bool = False) -> Graph:
    """The graph of the events in these `trace.jsonl` files.

    A span's start and end events become one node: the attrs are merged with the end event
    winning, `phase` is dropped, and `status` and `dur_s` are taken from the end. A point
    event's `ts` becomes `start_ts` and its `status` and `dur_s` stay None. Parent links
    that name a node that is not in the graph leave the child a root and add a warning.
    Kinds starting with `trace.JUDGE_PREFIX` are read only when `include_judge` is true.
    """
    graph = Graph()
    for raw in paths:
        path = Path(raw)
        for event in trace_file.read(path):
            if not include_judge and event.kind.startswith(trace.JUDGE_PREFIX):
                continue
            key = (event.run_id, event.span_id)
            node = graph.nodes.get(key)
            if node is None:
                node = Node(
                    key=key,
                    kind=event.kind,
                    run_id=event.run_id,
                    span_id=event.span_id,
                    parent=_parent_key(event),
                    attrs={},
                    start_ts=None,
                    end_ts=None,
                    status=None,
                    dur_s=None,
                    source=path,
                )
                graph.nodes[key] = node
            elif node.parent is None:
                node.parent = _parent_key(event)
            phase = event.attrs.get("phase")
            if phase == "end":
                node.end_ts = event.ts
                node.status = event.attrs.get("status")
                node.dur_s = event.attrs.get("dur_s")
            elif node.start_ts is None:
                node.start_ts = event.ts
            for name, value in event.attrs.items():
                if name != "phase":
                    node.attrs[name] = value
    graph.edges = _edges(graph)
    graph._reindex()
    return graph


def _parent_key(event: trace.TraceEvent) -> tuple[str, str] | None:
    """The parent key of the event's span, or None on the root."""
    if event.parent_id is None:
        return None
    return (event.run_id, event.parent_id)


def _edges(graph: Graph) -> list[Edge]:
    """One `contains` edge per kept parent link; a missing parent leaves a root and a warning."""
    edges: list[Edge] = []
    for key in sorted(graph.nodes):
        node = graph.nodes[key]
        if node.parent is None:
            continue
        if node.parent in graph.nodes:
            edges.append(Edge(node.parent, key))
            continue
        run_id, span_id = node.parent
        graph.warnings.append(
            f"missing parent {run_id}/{span_id} of {node.kind} {key[0]}/{key[1]} in {node.source}"
        )
        node.parent = None
    return edges


def load_runs(root: Path, *, include_judge: bool = False) -> tuple[Graph, dict[Path, dict]]:
    """The graph of every `trace.jsonl` under `root` and the summaries next to them.

    The summaries are keyed by the task run directory; a directory without a
    `summary.json` is left out. A summary that cannot be read is a graph warning.
    """
    base = Path(root)
    paths = sorted(path for path in base.rglob(TRACE_NAME) if path.is_file())
    graph = build(paths, include_judge=include_judge)
    summaries: dict[Path, dict] = {}
    for path in paths:
        summary_path = path.parent / SUMMARY_NAME
        if not summary_path.is_file():
            continue
        try:
            summaries[path.parent] = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            graph.warnings.append(f"cannot read {summary_path}: {exc}")
    return graph, summaries
