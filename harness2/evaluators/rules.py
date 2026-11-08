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

"""The bundled `rules` evaluator: deterministic checks that need no model.

Every check reads only the run graph and the `summary.json` next to each trace, so the same
request gives the same findings. The checks follow the table in `docs/specs/eval-graph.md`
("同梱の rules 評価者"):

- budget: each mode's ledger holds the R = 4k rollouts and P = 3k improvement stages, and the
  MODE span contains the same counts (the k base rollouts live under the TASK span). A ledger
  whose counts are missing or not integers, and a requested mode that never ran, are findings.
- cost: the sum of a task's ledger `rollout_usd`, with the shared base rollouts counted once,
  agrees with the sum of `models.executor[].cost_usd`.
- reproducibility: a task stays within the model-switch and stage-attempt limits, and errors
  are counted per `where` across the tasks of the request, including a span that ended ok yet
  caught a failure.
- safety: a fenced candidate is reported, at `info` only when every fence of the task recovered;
  a fence recovers when a stage that did not fall back contains it or started after it and ended
  successfully.

The two checks that read `summary.json` (the ledger identity and the cost agreement) run once
per task run directory, not once per run: a resumed run adds a second run to the same trace and
reads the same summary. Their graph is the last run of that trace that is not a resume, and
their key names the task and the task run directory, so one wrong ledger gives one finding while
the same task in two experiments stays two findings.

Every node is read once into an index by run and kind, so the checks do not walk the whole graph
again for each run.

Scores are never read: `request.scores` stays untouched, because the run decides whether the
evaluation may see them.
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from ..core import evaluate, trace
from ..core.evaluate import EvalRequest, Evidence, Finding, Severity, Viewpoint
from ..core.graph import Graph, Node

NAME = "rules"
VIEWPOINT_NAMES = ("cost", "budget", "safety", "reproducibility")

Index = Mapping[tuple[str, str], list[Node]]


class RulesEvaluator:
    """Deterministic checks over the run graph and the summaries of one request.

    `max_switches` and `max_attempts` are the limits a task may reach before a finding; a value
    has to exceed them. `cost_tolerance_usd` and `cost_tolerance_ratio` set how far the ledger
    and the executor records may differ: the larger of the fixed amount and the ratio times the
    larger total is allowed.
    """

    def __init__(
        self,
        *,
        max_switches: int = 3,
        max_attempts: int = 2,
        cost_tolerance_usd: float = 0.01,
        cost_tolerance_ratio: float = 0.01,
    ) -> None:
        self.max_switches = int(max_switches)
        self.max_attempts = int(max_attempts)
        self.cost_tolerance_usd = float(cost_tolerance_usd)
        self.cost_tolerance_ratio = float(cost_tolerance_ratio)

    def viewpoints(self) -> list[Viewpoint]:
        """The four angles this evaluator judges: cost, budget, safety, reproducibility."""
        return [evaluate.viewpoint(name) for name in VIEWPOINT_NAMES]

    def evaluate(self, request: EvalRequest) -> list[Finding]:
        """Every check of the table, one finding per distinct problem; scores are not read."""
        findings: list[Finding] = []
        graph = request.graph
        index = _node_index(graph)
        runs = graph.runs()
        for run_id, task in sorted(runs.items()):
            summary = request.summaries.get(task.source.parent)
            if not isinstance(summary, Mapping):
                summary = {}
            task_name = _task_name(run_id, task, summary)
            findings.extend(self._budget_counts(index, graph, run_id, task, summary, task_name))
            findings.extend(self._switches(index, run_id, task_name))
            findings.extend(self._attempts(index, run_id, task_name))
            findings.extend(self._fences(index, graph, run_id, task_name))
        by_path: dict[Path, list[Node]] = defaultdict(list)
        for task in runs.values():
            by_path[task.source.parent].append(task)
        for path, summary in sorted(request.summaries.items()):
            if not isinstance(summary, Mapping):
                continue
            run_id, task = _summary_run(by_path.get(path, []))
            if run_id is None or task is None:
                continue
            task_name = _summary_name(summary, path)
            findings.extend(
                self._budget_ledger(index, graph, run_id, path, task, summary, task_name)
            )
            findings.extend(self._cost(index, run_id, path, task, summary, task_name))
        findings.extend(self._errors(graph))
        return findings

    # -- checks ---------------------------------------------------------------------------

    def _budget_ledger(
        self,
        index: Index,
        graph: Graph,
        run_id: str,
        path: Path,
        task: Node,
        summary: Mapping[str, Any],
        task_name: str,
    ) -> list[Finding]:
        """The ledger identity of each mode, once per task run directory."""
        findings = []
        k = _as_int(summary.get("k"))
        if k is None:
            k = _as_int(task.attrs.get("k"))
        modes = _summary_modes(summary)
        graph_modes = _mode_nodes(index, run_id)
        key = f"rules:budget:ledger:{task_name}:{path}"

        for mode in sorted(modes):
            record = modes[mode]
            ledger = record.get("ledger") if isinstance(record, Mapping) else None
            if not isinstance(ledger, Mapping) or not ledger:
                continue
            recorded_r = _as_int(ledger.get("R"))
            expected_r = _as_int(ledger.get("expected_R"))
            recorded_p = _as_int(ledger.get("P"))
            expected_p = _as_int(ledger.get("expected_P"))
            wanted_r = 4 * k if k is not None else expected_r
            wanted_p = 3 * k if k is not None else expected_p
            node = graph_modes.get(str(mode))
            evidence = _ledger_evidence(graph, node, task)
            if recorded_r is None or expected_r is None or recorded_p is None or expected_p is None:
                findings.append(
                    self._finding(
                        "budget",
                        Severity.MAJOR,
                        f"The ledger of mode {mode} of task {task_name} is missing R, P, "
                        f"expected_R or expected_P, or they are not integers.",
                        evidence,
                        "Record integer R, P, expected_R and expected_P in the ledger so the "
                        "budget identity can be checked.",
                        f"{key}:{mode}",
                    )
                )
                continue
            if (
                recorded_r == expected_r
                and recorded_p == expected_p
                and expected_r == wanted_r
                and expected_p == wanted_p
            ):
                continue
            if k is None:
                claim = (
                    f"Mode {mode} of task {task_name} records R={recorded_r} against "
                    f"expected_R={expected_r} and P={recorded_p} against "
                    f"expected_P={expected_p}."
                )
            else:
                claim = (
                    f"Mode {mode} of task {task_name} records R={recorded_r} and P={recorded_p}, "
                    f"but a k={k} run must record R={wanted_r} and P={wanted_p}."
                )
            findings.append(
                self._finding(
                    "budget",
                    Severity.MAJOR,
                    claim,
                    evidence,
                    "Record every rollout and improvement stage of the mode in its ledger so "
                    "the budget identity holds.",
                    f"{key}:{mode}",
                )
            )
        return findings

    def _budget_counts(
        self,
        index: Index,
        graph: Graph,
        run_id: str,
        task: Node,
        summary: Mapping[str, Any],
        task_name: str,
    ) -> list[Finding]:
        """The counts under each MODE span of one run, against the 4k and 3k of its ledger."""
        findings = []
        modes = _summary_modes(summary)
        graph_modes = _mode_nodes(index, run_id)
        requested = task.attrs.get("modes")
        if not task.attrs.get("resumed") and isinstance(requested, (list, tuple)):
            for mode in sorted({str(item) for item in requested}):
                if mode in graph_modes or _ledger_of(modes, mode):
                    continue
                findings.append(
                    self._finding(
                        "budget",
                        Severity.MAJOR,
                        f"Mode {mode} of task {task_name} was requested but has neither a MODE "
                        f"span nor a ledger.",
                        [_evidence(task)],
                        "Rerun the missing mode, or fix the modes the task records.",
                        f"rules:budget:missing:{run_id}:{mode}",
                    )
                )
        k = _as_int(summary.get("k"))
        if k is None:
            k = _as_int(task.attrs.get("k"))
        if k is None:
            return findings
        wanted_r, wanted_p = 4 * k, 3 * k
        base_count = sum(
            1
            for node in _nodes(index, run_id, trace.Kind.ROLLOUT)
            if node.attrs.get("role") == "base"
        )
        for mode, node in sorted(graph_modes.items()):
            if node.attrs.get("resumed"):
                continue
            children = graph.children(node.key)
            rollouts = [
                child
                for child in children
                if child.kind == trace.Kind.ROLLOUT
                and child.attrs.get("role") in ("candidate", "final")
            ]
            stages = [child for child in children if child.kind == trace.Kind.STAGE]
            got_r = len(rollouts) + base_count
            got_p = len(stages)
            if got_r == wanted_r and got_p == wanted_p:
                continue
            evidence = [_evidence(node)]
            evidence.extend(_evidence(child) for child in (rollouts[:2] + stages[:2]))
            findings.append(
                self._finding(
                    "budget",
                    Severity.MAJOR,
                    f"Mode {mode} of task {task_name} shows {got_r} rollouts and {got_p} stages "
                    f"in the trace, not the R={wanted_r} and P={wanted_p} a k={k} run must "
                    f"record.",
                    evidence,
                    "Run and trace the rollouts and stages the budget expects, or fix the k and "
                    "the counts the run records.",
                    f"rules:budget:graph:{run_id}:{mode}",
                )
            )
        return findings

    def _cost(
        self,
        index: Index,
        run_id: str,
        path: Path,
        task: Node,
        summary: Mapping[str, Any],
        task_name: str,
    ) -> list[Finding]:
        """The ledger's rollout cost against the executor records of the summary."""
        if "models" not in summary:
            return []
        models = summary.get("models")
        if not isinstance(models, Mapping):
            return []
        rows = models.get("executor")
        if not isinstance(rows, (list, tuple)):
            rows = []
        models_usd = 0.0
        for row in rows:
            if isinstance(row, Mapping):
                models_usd += _as_float(row.get("cost_usd")) or 0.0
        ledgers: list[Mapping[str, Any]] = []
        for record in _summary_modes(summary).values():
            ledger = record.get("ledger") if isinstance(record, Mapping) else None
            if isinstance(ledger, Mapping):
                ledgers.append(ledger)
        if not ledgers:
            return []
        ledger_usd = 0.0
        for ledger in ledgers:
            ledger_usd += _as_float(ledger.get("rollout_usd")) or 0.0
        base_rollouts = [
            node
            for node in _nodes(index, run_id, trace.Kind.ROLLOUT)
            if node.attrs.get("role") == "base"
        ]
        base_usd = 0.0
        for node in base_rollouts:
            base_usd += _as_float(node.attrs.get("usd")) or 0.0
        if base_rollouts:
            ledger_usd -= base_usd * (len(ledgers) - 1)
        ledger_usd = round(ledger_usd, 6)
        larger = max(abs(ledger_usd), abs(models_usd))
        tolerance = max(self.cost_tolerance_usd, self.cost_tolerance_ratio * larger)
        split_zero = (ledger_usd == 0.0) != (models_usd == 0.0)
        if not split_zero and abs(ledger_usd - models_usd) <= tolerance:
            return []
        evidence = [_evidence(task)]
        evidence.extend(_evidence(node) for node in _nodes(index, run_id, trace.Kind.COST))
        claim = (
            f"The ledger of task {task_name} records {ledger_usd:.6f} USD of rollouts but "
            f"the executor records hold {models_usd:.6f} USD"
        )
        if base_rollouts:
            claim += "."
        else:
            claim += (
                ", and the base rollout cost could not be subtracted because the trace has no "
                "base rollout."
            )
        return [
            self._finding(
                "cost",
                Severity.MAJOR,
                claim,
                evidence,
                "Record each rollout's cost in both the ledger and the executor records so the "
                "two totals agree.",
                f"rules:cost:{task_name}:{path}",
            )
        ]

    def _switches(self, index: Index, run_id: str, task_name: str) -> list[Finding]:
        """A task that switched models more often than `max_switches`."""
        nodes = _nodes(index, run_id, trace.Kind.MODEL_SWITCH)
        if len(nodes) <= self.max_switches:
            return []
        return [
            self._finding(
                "reproducibility",
                Severity.MINOR,
                f"Task {task_name} switched models {len(nodes)} times, more than the "
                f"{self.max_switches} allowed.",
                [_evidence(node) for node in nodes],
                "Keep the executor on one reachable model, or raise max_switches when switching "
                "is expected.",
                f"rules:switches:{run_id}",
            )
        ]

    def _attempts(self, index: Index, run_id: str, task_name: str) -> list[Finding]:
        """Stages whose recorded attempts exceed `max_attempts`."""
        nodes = []
        for node in _nodes(index, run_id, trace.Kind.STAGE):
            attempts = _as_int(node.attrs.get("attempts"))
            if attempts is not None and attempts > self.max_attempts:
                nodes.append(node)
        if not nodes:
            return []
        return [
            self._finding(
                "reproducibility",
                Severity.MINOR,
                f"Task {task_name} ran {_counted(len(nodes), 'improvement stage')} with more "
                f"than {self.max_attempts} attempts.",
                [_evidence(node) for node in nodes],
                "Make the improver deliver an acceptable tree on its first or second attempt.",
                f"rules:attempts:{run_id}",
            )
        ]

    def _fences(self, index: Index, graph: Graph, run_id: str, task_name: str) -> list[Finding]:
        """A fenced candidate, weighed by whether every fence of the task recovered."""
        fences = [
            node
            for node in _nodes(index, run_id, trace.Kind.MODEL_FENCE)
            if node.attrs.get("fenced") is False
        ]
        if not fences:
            return []
        stages = _nodes(index, run_id, trace.Kind.STAGE)
        if all(_fence_recovered(graph, stages, fence) for fence in fences):
            severity = Severity.INFO
            claim = (
                f"Task {task_name} fenced {_counted(len(fences), 'model candidate')}, and every "
                f"fence was recovered."
            )
            suggestion = (
                "Check the fence of the skipped runner before relying on the model that ran."
            )
        else:
            severity = Severity.MAJOR
            claim = (
                f"Task {task_name} fenced {_counted(len(fences), 'model candidate')}, and at "
                f"least one fence was not recovered."
            )
            suggestion = (
                "Give the improver a fenced runner, or fix the fence check, so the improvement "
                "can run."
            )
        return [
            self._finding(
                "safety",
                severity,
                claim,
                [_evidence(node) for node in fences],
                suggestion,
                f"rules:fence:{run_id}",
            )
        ]

    def _errors(self, graph: Graph) -> list[Finding]:
        """Error events and caught failures counted by `where` across the tasks of the request."""
        failed = [node for node in graph.nodes.values() if _is_failure(node)]
        mode_errors: set[tuple[str, str]] = set()
        for node in failed:
            if node.kind != trace.Kind.ERROR:
                continue
            where = str(node.attrs.get("where") or "")
            if where.startswith("mode:"):
                mode_errors.add((node.run_id, where.removeprefix("mode:")))
        groups: dict[str, list[Node]] = defaultdict(list)
        for node in sorted(failed, key=lambda item: item.key):
            if (
                node.kind == trace.Kind.MODE
                and (
                    node.run_id,
                    str(node.attrs.get("mode") or ""),
                )
                in mode_errors
            ):
                continue
            where = node.attrs.get("where") or node.kind
            groups[str(where)].append(node)
        tasks = graph.runs()
        findings = []
        for where in sorted(groups):
            nodes = groups[where]
            task_count = len({_task_id(tasks.get(node.run_id), node.run_id) for node in nodes})
            severity = Severity.MINOR if task_count >= 2 else Severity.INFO
            findings.append(
                self._finding(
                    "reproducibility",
                    severity,
                    f"Errors at {where!r} appeared in {_counted(task_count, 'task')}.",
                    [_evidence(node) for node in nodes[:10]],
                    f"Fix the cause at {where!r} so the next run stops failing there.",
                    f"rules:errors:{where}",
                )
            )
        return findings

    # -- helpers --------------------------------------------------------------------------

    def _finding(
        self,
        viewpoint: str,
        severity: str,
        claim: str,
        evidence: list[Evidence],
        suggestion: str,
        key: str,
    ) -> Finding:
        """One finding of this evaluator, with the tuple of evidence its checks collected."""
        return Finding(
            viewpoint=viewpoint,
            severity=severity,
            claim=claim,
            evidence=tuple(evidence),
            suggestion=suggestion,
            key=key,
            evaluator=NAME,
        )


def _node_index(graph: Graph) -> dict[tuple[str, str], list[Node]]:
    """Every node by run and kind, each list in start order; built once per request."""
    index: dict[tuple[str, str], list[Node]] = defaultdict(list)
    for node in graph.nodes.values():
        index[(node.run_id, node.kind)].append(node)
    for nodes in index.values():
        nodes.sort(key=lambda node: (node.start_ts or "", node.span_id))
    return index


def _nodes(index: Index, run_id: str, kind: str) -> list[Node]:
    """The nodes of one run and kind, in start order."""
    return index.get((run_id, kind), [])


def _summary_modes(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    """The per-mode records of a summary: `modes`, else the legacy `arms` of an old summary."""
    if "modes" in summary:
        modes = summary.get("modes")
        return modes if isinstance(modes, Mapping) else {}
    arms = summary.get("arms")
    return arms if isinstance(arms, Mapping) else {}


def _ledger_of(modes: Mapping[str, Any], mode: str) -> Mapping[str, Any] | None:
    """The non-empty ledger of `mode`, or None."""
    record = modes.get(mode)
    ledger = record.get("ledger") if isinstance(record, Mapping) else None
    if isinstance(ledger, Mapping) and ledger:
        return ledger
    return None


def _ledger_evidence(graph: Graph, node: Node | None, task: Node) -> list[Evidence]:
    """The MODE span and its COST children, else the TASK node."""
    evidence = []
    if node is not None:
        evidence.append(_evidence(node))
        evidence.extend(
            _evidence(child) for child in graph.children(node.key) if child.kind == trace.Kind.COST
        )
    if not evidence:
        evidence.append(_evidence(task))
    return evidence


def _task_name(run_id: str, task: Node, summary: Mapping[str, Any]) -> str:
    """The task id from the summary, the TASK attrs or the run id, in that order."""
    return str(summary.get("task_id") or task.attrs.get("task_id") or run_id)


def _summary_name(summary: Mapping[str, Any], path: Path) -> str:
    """The task id of a summary, else the name of the task run directory."""
    return str(summary.get("task_id") or path.name)


def _task_id(task: Node | None, run_id: str) -> str:
    """The task id of a run: the TASK attrs' `task_id`, else the run id."""
    return str((task.attrs.get("task_id") if task is not None else None) or run_id)


def _summary_run(tasks: list[Node]) -> tuple[str | None, Node | None]:
    """The run whose graph to read a summary with: the last run that is not a resume."""
    if not tasks:
        return None, None
    fresh = [node for node in tasks if not node.attrs.get("resumed")]
    chosen = max(fresh or tasks, key=lambda node: (node.start_ts or "", node.span_id))
    return chosen.run_id, chosen


def _mode_nodes(index: Index, run_id: str) -> dict[str, Node]:
    """The first MODE span of each mode in one run, by mode name."""
    found: dict[str, Node] = {}
    for node in _nodes(index, run_id, trace.Kind.MODE):
        found.setdefault(str(node.attrs.get("mode") or ""), node)
    return found


def _is_after(node: Node, earlier: Node) -> bool:
    """Whether `node` started after `earlier`; a missing timestamp is not after anything."""
    if node.start_ts is None or earlier.start_ts is None:
        return False
    return node.start_ts > earlier.start_ts


def _fence_recovered(graph: Graph, stages: list[Node], fence: Node) -> bool:
    """Whether a stage that did not fall back contains the fence or started after it."""
    if any(_ended_ok(stage) and _is_after(stage, fence) for stage in stages):
        return True
    for ancestor in graph.ancestors(fence.key):
        if ancestor.kind == trace.Kind.STAGE:
            return _ended_ok(ancestor)
    return False


def _counted(number: int, noun: str) -> str:
    """`1 task` but `2 tasks`, for the claims."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _evidence(node: Node) -> Evidence:
    """The evidence pointing at this node."""
    return Evidence(run_id=node.run_id, span_id=node.span_id)


def _ended_ok(node: Node) -> bool:
    """Whether a stage ended successfully: no error, no fallback, and ok not false."""
    if node.status != "ok":
        return False
    if node.attrs.get("error"):
        return False
    if node.attrs.get("ok") is False:
        return False
    return not node.attrs.get("fell_back")


def _is_failure(node: Node) -> bool:
    """Whether a node records a failure: an ERROR, a failed span, or a caught one."""
    if node.kind == trace.Kind.ERROR or node.status == "error":
        return True
    if node.status == "ok" and (node.attrs.get("error") or node.attrs.get("ok") is False):
        return True
    return False


def _as_int(value: Any) -> int | None:
    """`value` when it is an int and not a bool, else None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_float(value: Any) -> float | None:
    """`value` as a finite float when it is a real number and not a bool, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
