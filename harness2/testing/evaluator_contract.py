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

"""Contract checks for evaluators, for evaluator authors to run from their own tests.

The evaluator contract is described in `harness2.core.evaluate`: `viewpoints()` returns a
non-empty sequence of `Viewpoint` whose names are short identifiers, and `evaluate(request)`
returns a list of `Finding` whose viewpoint is one of the evaluator's own and whose evidence
points at spans of the request graph. `evaluate` must work with `request.scores` set to `None`
and to `{}`, because a run allows scores only when it says so.

- `check_evaluator(ev, request=None)` raises AssertionError when `ev` is not an `Evaluator`, when
  `viewpoints()` is empty or holds a name that is not `[a-z][a-z0-9_]*`, and, with a `request`,
  when `evaluate` returns anything but a list of `Finding` that follow the rules above. It
  returns the findings of the `scores=None` run.
- `contract_request(root, *, scores=None)` is the small request `check_evaluator` and the mixin
  use by default: one TASK with two children, written with `trace.Tracer` and
  `trace_file.FileSink` under `root`, with `VIEWPOINTS` as its viewpoints.
- `EvaluatorContractTests` is a unittest mixin. `setUp` builds `self.request` first, then calls
  `make_evaluator()`; an evaluator whose findings must point at real spans reads
  `self.request.graph` there, because span ids are new on every run.
- `FixedEvaluator` returns the findings it was given and takes its viewpoints from them, so a
  test can hand it a finding and get a contract-conforming evaluator.

Example::

    import unittest
    from pathlib import Path

    from harness2.core.evaluate import Evidence, Finding, Severity
    from harness2.testing.evaluator_contract import EvaluatorContractTests

    from my_package.evaluator import MyEvaluator


    class MyEvaluatorContractTest(EvaluatorContractTests, unittest.TestCase):
        def make_evaluator(self):
            return MyEvaluator()

The mixin does not inherit from `unittest.TestCase`, so unittest does not collect it on its
own; list `unittest.TestCase` after it in your test class. A subclass that overrides `setUp`
must call `super().setUp()`.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from harness2.core import evaluate, graph, trace
from harness2.core.evaluate import EvalRequest, Evaluator, Finding, Viewpoint
from harness2.core.trace_file import FILE_NAME, FileSink

_RUN_ID = "contract"
_VIEWPOINT_NAME = re.compile(r"[a-z][a-z0-9_]*")


def contract_request(root: Path, *, scores: Any = None) -> EvalRequest:
    """A small request for contract checks: one TASK with two children under `root`.

    The trace is written to `root / "contract" / "trace.jsonl"` with `trace.Tracer` and
    `trace_file.FileSink`. `summaries` holds one minimal summary for that directory and
    `viewpoints` is `evaluate.VIEWPOINTS`.
    """
    task_dir = Path(root) / "contract"
    tracer = trace.Tracer(FileSink(task_dir), run_id=_RUN_ID)
    try:
        with trace.use(tracer):
            with trace.span(trace.Kind.TASK):
                with trace.span(trace.Kind.ROLLOUT, role="candidate"):
                    pass
                with trace.span(trace.Kind.STAGE, attempts=1):
                    pass
    finally:
        tracer.close()
    request_graph = graph.build([task_dir / FILE_NAME])
    return EvalRequest(
        graph=request_graph,
        summaries={task_dir: {"task_id": _RUN_ID}},
        viewpoints=evaluate.VIEWPOINTS,
        scores=scores,
    )


def check_evaluator(ev: Any, request: EvalRequest | None = None) -> list[Finding]:
    """Assert that `ev` meets the evaluator contract; return its findings when checked.

    The checks on `viewpoints()` always run. With a `request`, `evaluate` runs with `scores`
    set to `None` and to `{}`, and every finding must name one of the evaluator's viewpoints
    and point its evidence at a span of `request.graph`; the findings of the `scores=None` run
    are returned. Without a request no `evaluate` runs and `[]` is returned.
    """
    problems = [
        name for name in ("viewpoints", "evaluate") if not callable(getattr(ev, name, None))
    ]
    if problems or not isinstance(ev, Evaluator):
        detail = ", ".join(problems) or "the Evaluator Protocol check failed"
        raise AssertionError(
            f"{type(ev).__name__} is not an Evaluator; missing or invalid: {detail}"
        )
    declared = ev.viewpoints()
    _check_viewpoints(declared)
    if request is None:
        return []
    allowed = {item.name for item in declared}
    findings = _check_findings(ev, replace(request, scores=None), allowed)
    _check_findings(ev, replace(request, scores={}), allowed)
    return findings


def _check_viewpoints(declared: Any) -> None:
    """Assert that `declared` is a non-empty sequence of `Viewpoint` with usable names."""
    if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)) or not declared:
        raise AssertionError("viewpoints() must return a non-empty sequence of Viewpoint")
    for item in declared:
        if not isinstance(item, Viewpoint):
            raise AssertionError(
                f"viewpoints() must return Viewpoint objects, not {type(item).__name__}"
            )
        if not isinstance(item.name, str) or not _VIEWPOINT_NAME.fullmatch(item.name):
            raise AssertionError(
                f"viewpoint name {item.name!r} must match [a-z][a-z0-9_]*: it becomes a file name"
            )


def _check_findings(ev: Evaluator, request: EvalRequest, allowed: set[str]) -> list[Finding]:
    """Run `evaluate` once and assert that its result obeys the contract."""
    result = ev.evaluate(request)
    if not isinstance(result, list):
        raise AssertionError(
            f"evaluate() must return a list of Finding, not {type(result).__name__}"
        )
    for finding in result:
        if not isinstance(finding, Finding):
            raise AssertionError(f"evaluate() returned {type(finding).__name__}, not a Finding")
        if finding.viewpoint not in allowed:
            raise AssertionError(
                f"finding viewpoint {finding.viewpoint!r} is not one of "
                f"{sorted(allowed)}: findings of another viewpoint are dropped"
            )
        for item in finding.evidence:
            if request.graph.find(item.run_id, item.span_id) is None:
                raise AssertionError(
                    f"finding evidence {item.run_id}/{item.span_id} is not a span of the "
                    "request graph: the aggregation drops a finding without evidence"
                )
    return result


class FixedEvaluator:
    """An evaluator that returns the findings it was given, for tests.

    `viewpoints` defaults to the `VIEWPOINTS` that the findings name, and to all of them when
    the findings are empty. Pass `viewpoints` to declare another set.
    """

    def __init__(
        self,
        findings: Sequence[Finding] = (),
        viewpoints: Sequence[Viewpoint] | None = None,
    ) -> None:
        self._findings = tuple(findings)
        self._viewpoints = None if viewpoints is None else tuple(viewpoints)

    def viewpoints(self) -> list[Viewpoint]:
        """The declared viewpoints, or those the findings name (all six when none)."""
        if self._viewpoints is not None:
            return list(self._viewpoints)
        used = {finding.viewpoint for finding in self._findings}
        chosen = [item for item in evaluate.VIEWPOINTS if item.name in used]
        return chosen or list(evaluate.VIEWPOINTS)

    def evaluate(self, request: EvalRequest) -> list[Finding]:
        """A new list of the findings this evaluator was given."""
        return list(self._findings)


class EvaluatorContractTests:
    """A unittest mixin that checks an evaluator against the evaluator contract.

    Subclass it together with `unittest.TestCase` and implement `make_evaluator()`. `setUp`
    builds `self.request` first with `make_request`, so `make_evaluator` may read its graph and
    point evidence at spans that exist.
    """

    request: EvalRequest

    def make_request(self, root: Path) -> EvalRequest:
        """Build the request under test for `root`; override for a richer graph."""
        return contract_request(root)

    def make_evaluator(self) -> Evaluator:
        """Build the evaluator under test; may read `self.request.graph`."""
        raise NotImplementedError("subclasses of EvaluatorContractTests implement make_evaluator")

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)  # type: ignore[attr-defined]
        self.request = self.make_request(Path(holder.name))
        self.evaluator = self.make_evaluator()

    # -- tests ---------------------------------------------------------------------------

    def test_evaluator_contract_check_evaluator(self) -> None:
        check_evaluator(self.evaluator)

    def test_evaluator_contract_evaluate_returns_findings(self) -> None:
        findings = check_evaluator(self.evaluator, self.request)
        if not isinstance(findings, list):
            raise AssertionError(f"check_evaluator must return a list, got {type(findings)}")

    def test_evaluator_contract_scores_none_and_empty(self) -> None:
        check_evaluator(self.evaluator, replace(self.request, scores=None))
        check_evaluator(self.evaluator, replace(self.request, scores={}))
