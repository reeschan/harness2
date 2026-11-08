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

"""Tests for `harness2.evaluators.rules`: one check at a time, plus its contract."""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness2.core import evalgraph, evaluate, graph, trace
from harness2.core.evaluate import EvalRequest, Severity
from harness2.core.trace_file import FILE_NAME, FileSink
from harness2.evaluators.rules import RulesEvaluator
from harness2.testing.evaluator_contract import EvaluatorContractTests


def write_run(task_dir: Path, run_id: str, body) -> None:
    """Write one run of `body` into `task_dir`; a second call appends a second run."""
    tracer = trace.Tracer(FileSink(task_dir), run_id=run_id)
    try:
        with trace.use(tracer):
            body()
    finally:
        tracer.close()


def build_request(root: Path, runs, *, scores=None) -> EvalRequest:
    """Write each (run_id, body, summary) under its own directory and build one request."""
    paths = []
    summaries = {}
    for run_id, body, summary in runs:
        task_dir = Path(tempfile.mkdtemp(dir=root, prefix=f"{run_id}-"))
        write_run(task_dir, run_id, body)
        paths.append(task_dir / FILE_NAME)
        summaries[task_dir] = summary
    return EvalRequest(
        graph=graph.build(paths),
        summaries=summaries,
        viewpoints=evaluate.VIEWPOINTS,
        scores=scores,
    )


def build_resumed_request(root: Path, runs, summary) -> EvalRequest:
    """Write several runs into one task run directory, as a resume does, and build a request."""
    task_dir = Path(tempfile.mkdtemp(dir=root, prefix="resumed-"))
    for run_id, body in runs:
        write_run(task_dir, run_id, body)
    return EvalRequest(
        graph=graph.build([task_dir / FILE_NAME]),
        summaries={task_dir: summary},
        viewpoints=evaluate.VIEWPOINTS,
    )


def emit_task(k: int = 1, *, bases=None, candidates=None, finals=None, stages=None):
    """A TASK with the given counts; the candidate and final rollouts sit under one MODE."""
    bases = k if bases is None else bases
    candidates = 2 * k if candidates is None else candidates
    finals = k if finals is None else finals
    stages = 3 * k if stages is None else stages

    def body():
        with trace.span(trace.Kind.TASK, task_id="task-1", k=k) as task_span:
            task_span.set(resumed=False)
            for index in range(bases):
                with trace.span(trace.Kind.ROLLOUT, role="base", mode="", label=f"base{index}"):
                    pass
            with trace.span(trace.Kind.MODE, mode="agg"):
                for index in range(candidates):
                    with trace.span(
                        trace.Kind.ROLLOUT, role="candidate", mode="agg", label=f"c{index}"
                    ):
                        pass
                for index in range(finals):
                    with trace.span(
                        trace.Kind.ROLLOUT, role="final", mode="agg", label=f"f{index}"
                    ):
                        pass
                for index in range(stages):
                    with trace.span(
                        trace.Kind.STAGE, label=f"agg{index}", dim="compose", mode="agg"
                    ) as stage_span:
                        stage_span.set(
                            kept_base=False,
                            fell_back=False,
                            attempts=1,
                            cached=False,
                            usd=0.0,
                            error="",
                        )
                trace.event(
                    trace.Kind.COST,
                    mode="agg",
                    usd=0.5,
                    rollout_usd=0.5,
                    session_usd=0.0,
                    R=4 * k,
                    P=3 * k,
                )

    return body


def make_summary(k: int = 1, *, usd=0.5, include_k=True, models=True, ledger=True):
    """The healthy summary that matches `emit_task`."""
    modes = {}
    if ledger:
        modes["agg"] = {
            "ok": True,
            "ledger": {
                "R": 4 * k,
                "expected_R": 4 * k,
                "P": 3 * k,
                "expected_P": 3 * k,
                "rollout_usd": usd,
            },
        }
    summary = {"task_id": "task-1", "status": "ok", "modes": modes}
    if include_k:
        summary["k"] = k
    if models:
        summary["models"] = {"executor": [{"cost_usd": usd}]}
    return summary


def emit_two_modes(*, bases=1, resumed=False):
    """A healthy k=1 task with modes agg and seq; the base costs 1.0 and each mode 3.0."""

    def body():
        with trace.span(trace.Kind.TASK, task_id="task-1", k=1) as task_span:
            task_span.set(resumed=resumed)
            for index in range(bases):
                with trace.span(
                    trace.Kind.ROLLOUT, role="base", mode="", label=f"base{index}"
                ) as base:
                    base.set(ok=True, usd=1.0)
            for mode in ("agg", "seq"):
                with trace.span(trace.Kind.MODE, mode=mode, resumed=resumed):
                    for index in range(2):
                        with trace.span(
                            trace.Kind.ROLLOUT, role="candidate", mode=mode, label=f"c{index}"
                        ) as candidate:
                            candidate.set(ok=True, usd=1.0)
                    with trace.span(
                        trace.Kind.ROLLOUT, role="final", mode=mode, label="f0"
                    ) as final:
                        final.set(ok=True, usd=1.0)
                    for index in range(3):
                        with trace.span(
                            trace.Kind.STAGE, label=f"{mode}{index}", dim="compose", mode=mode
                        ) as stage:
                            stage.set(
                                kept_base=False,
                                fell_back=False,
                                attempts=1,
                                cached=False,
                                usd=0.0,
                                error="",
                            )
                    trace.event(
                        trace.Kind.COST,
                        mode=mode,
                        usd=3.0,
                        rollout_usd=3.0,
                        session_usd=0.0,
                        R=4,
                        P=3,
                    )

    return body


def make_two_mode_summary(*, ledger_usd=4.0, models_usd=7.0):
    """The summary that matches `emit_two_modes`: each ledger counts the shared base."""

    def mode_record():
        return {
            "ok": True,
            "ledger": {
                "R": 4,
                "expected_R": 4,
                "P": 3,
                "expected_P": 3,
                "rollout_usd": ledger_usd,
            },
        }

    return {
        "task_id": "task-1",
        "k": 1,
        "modes": {"agg": mode_record(), "seq": mode_record()},
        "models": {"executor": [{"cost_usd": models_usd}]},
    }


def emit_requested_modes(*, modes=("agg", "seq"), ran=("agg",), resumed=False):
    """A k=1 task that asked for `modes` but traced only `ran`, as a killed process leaves it."""

    def body():
        with trace.span(trace.Kind.TASK, task_id="task-1", k=1, modes=list(modes)) as task_span:
            task_span.set(resumed=resumed)
            with trace.span(trace.Kind.ROLLOUT, role="base", mode="", label="base0") as base:
                base.set(usd=1.0)
            for mode in ran:
                with trace.span(trace.Kind.MODE, mode=mode):
                    for index in range(2):
                        with trace.span(
                            trace.Kind.ROLLOUT, role="candidate", mode=mode, label=f"c{index}"
                        ):
                            pass
                    with trace.span(trace.Kind.ROLLOUT, role="final", mode=mode, label="f0"):
                        pass
                    for index in range(3):
                        with trace.span(
                            trace.Kind.STAGE, label=f"{mode}{index}", dim="compose", mode=mode
                        ) as stage:
                            stage.set(attempts=1, fell_back=False, error="")

    return body


class RulesTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.evaluator = RulesEvaluator()

    def findings(self, runs, *, scores=None, evaluator=None):
        request = build_request(self.root, runs, scores=scores)
        return (evaluator or self.evaluator).evaluate(request)


class ViewpointTests(unittest.TestCase):
    def test_the_four_viewpoints(self):
        self.assertEqual(
            [item.name for item in RulesEvaluator().viewpoints()],
            ["cost", "budget", "safety", "reproducibility"],
        )


class HealthyRunTests(RulesTestCase):
    def test_a_healthy_run_has_no_findings(self):
        self.assertEqual(self.findings([("run-1", emit_task(), make_summary())]), [])

    def test_k_comes_from_the_task_attrs_when_the_summary_has_none(self):
        runs = [("run-1", emit_task(k=2), make_summary(k=2, include_k=False, usd=1.0))]
        self.assertEqual(self.findings(runs), [])


class BudgetTests(RulesTestCase):
    def test_a_ledger_count_mismatch_is_major(self):
        summary = make_summary()
        summary["modes"]["agg"]["ledger"]["R"] = 3
        findings = self.findings([("run-1", emit_task(), summary)])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].viewpoint, "budget")
        self.assertEqual(findings[0].severity, Severity.MAJOR)
        self.assertIn("ledger", findings[0].key)

    def test_the_expected_counts_must_follow_k(self):
        summary = make_summary()
        summary["modes"]["agg"]["ledger"].update(R=3, expected_R=3, P=3, expected_P=3)
        findings = self.findings([("run-1", emit_task(), summary)])
        self.assertEqual(len(findings), 1)
        self.assertIn("ledger", findings[0].key)

    def test_a_trace_count_mismatch_is_major(self):
        findings = self.findings([("run-1", emit_task(candidates=1), make_summary())])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].viewpoint, "budget")
        self.assertIn("graph", findings[0].key)
        self.assertTrue(findings[0].evidence)

    def test_a_stage_count_mismatch_is_major(self):
        findings = self.findings([("run-1", emit_task(stages=2), make_summary())])
        self.assertEqual(len(findings), 1)
        self.assertIn("graph", findings[0].key)

    def test_a_resumed_run_does_not_repeat_the_ledger_finding(self):
        summary = make_two_mode_summary()
        summary["modes"]["agg"]["ledger"]["R"] = 3
        request = build_resumed_request(
            self.root,
            [("run-1", emit_two_modes()), ("run-2", emit_two_modes(bases=0, resumed=True))],
            summary,
        )
        findings = self.evaluator.evaluate(request)
        self.assertEqual(len(findings), 1)
        path = next(iter(request.summaries))
        self.assertEqual(findings[0].key, f"rules:budget:ledger:task-1:{path}:agg")

    def test_a_resumed_mode_is_not_counted(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1", k=1):
                with trace.span(trace.Kind.MODE, mode="agg", resumed=True):
                    pass

        summary = {"task_id": "task-1", "k": 1, "modes": {}}
        self.assertEqual(self.findings([("run-1", body, summary)]), [])

    def test_a_ledger_count_that_is_not_an_int_is_major(self):
        summary = make_summary()
        summary["modes"]["agg"]["ledger"]["R"] = "4"
        findings = self.findings([("run-1", emit_task(), summary)])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].viewpoint, "budget")
        self.assertEqual(findings[0].severity, Severity.MAJOR)
        self.assertIn("ledger", findings[0].key)

    def test_a_ledger_missing_a_count_is_major(self):
        summary = make_summary()
        del summary["modes"]["agg"]["ledger"]["expected_P"]
        findings = self.findings([("run-1", emit_task(), summary)])
        self.assertEqual(len(findings), 1)
        self.assertIn("ledger", findings[0].key)
        self.assertIn("expected_P", findings[0].claim)

    def test_a_ledger_without_k_but_with_a_string_count_is_major(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                with trace.span(trace.Kind.MODE, mode="agg"):
                    pass

        summary = {
            "task_id": "task-1",
            "modes": {
                "agg": {"ledger": {"R": "4", "P": "3", "expected_R": "4", "expected_P": "3"}}
            },
        }
        findings = self.findings([("run-1", body, summary)])
        self.assertEqual([finding.viewpoint for finding in findings], ["budget"])
        self.assertEqual(findings[0].severity, Severity.MAJOR)

    def test_a_requested_mode_with_no_span_and_no_ledger_is_major(self):
        summary = make_summary()
        findings = self.findings([("run-1", emit_requested_modes(), summary)])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].viewpoint, "budget")
        self.assertEqual(findings[0].severity, Severity.MAJOR)
        self.assertIn(":missing:", findings[0].key)
        self.assertIn("seq", findings[0].claim)

    def test_a_requested_mode_with_a_ledger_is_quiet(self):
        summary = make_two_mode_summary()
        findings = self.findings([("run-1", emit_requested_modes(), summary)])
        self.assertEqual(findings, [])

    def test_a_resumed_task_is_not_checked_for_a_missing_mode(self):
        summary = make_summary()
        findings = self.findings([("run-1", emit_requested_modes(resumed=True), summary)])
        self.assertEqual(findings, [])


class LegacySummaryTests(RulesTestCase):
    def test_a_ledger_under_arms_is_checked(self):
        summary = make_summary()
        summary["modes"]["agg"]["ledger"]["R"] = 3
        summary["arms"] = summary.pop("modes")
        findings = self.findings([("run-1", emit_task(), summary)])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].viewpoint, "budget")
        self.assertIn("ledger", findings[0].key)

    def test_the_cost_of_a_summary_under_arms_is_checked(self):
        summary = make_summary()
        summary["modes"]["agg"]["ledger"]["rollout_usd"] = 0.0
        summary["arms"] = summary.pop("modes")
        findings = self.findings([("run-1", emit_task(), summary)])
        self.assertEqual([finding.viewpoint for finding in findings], ["cost"])


class SummaryShapeTests(RulesTestCase):
    def test_a_summary_that_is_not_a_mapping_is_skipped(self):
        task_dir = Path(tempfile.mkdtemp(dir=self.root, prefix="bad-"))
        write_run(task_dir, "run-1", emit_task())
        request = EvalRequest(
            graph=graph.build([task_dir / FILE_NAME]),
            summaries={task_dir: [1, 2]},
            viewpoints=evaluate.VIEWPOINTS,
        )
        self.assertEqual(self.evaluator.evaluate(request), [])


class CostTests(RulesTestCase):
    def test_a_zero_ledger_against_positive_models_is_major(self):
        summary = make_summary()
        summary["modes"]["agg"]["ledger"]["rollout_usd"] = 0.0
        findings = self.findings([("run-1", emit_task(), summary)])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].viewpoint, "cost")
        self.assertEqual(findings[0].severity, Severity.MAJOR)
        self.assertEqual(len(findings[0].evidence), 2)

    def test_a_task_without_models_is_not_checked(self):
        summary = make_summary(models=False)
        summary["modes"]["agg"]["ledger"]["rollout_usd"] = 0.0
        self.assertEqual(self.findings([("run-1", emit_task(), summary)]), [])

    def test_a_difference_within_the_fixed_tolerance_is_quiet(self):
        summary = make_summary(usd=0.5)
        summary["models"]["executor"][0]["cost_usd"] = 0.505
        self.assertEqual(self.findings([("run-1", emit_task(), summary)]), [])

    def test_the_ratio_sets_the_tolerance_for_large_costs(self):
        close = make_summary(usd=100.0)
        close["models"]["executor"][0]["cost_usd"] = 100.5
        self.assertEqual(self.findings([("run-1", emit_task(), close)]), [])
        far = make_summary(usd=100.0)
        far["models"]["executor"][0]["cost_usd"] = 103.0
        self.assertEqual(len(self.findings([("run-1", emit_task(), far)])), 1)

    def test_the_shared_base_cost_counts_once_across_modes(self):
        runs = [("run-1", emit_two_modes(), make_two_mode_summary())]
        self.assertEqual(self.findings(runs), [])

    def test_a_two_mode_cost_mismatch_is_major(self):
        runs = [("run-1", emit_two_modes(), make_two_mode_summary(models_usd=5.0))]
        findings = self.findings(runs)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].viewpoint, "cost")
        self.assertEqual(findings[0].severity, Severity.MAJOR)
        self.assertIn("7.000000", findings[0].claim)
        self.assertIn("5.000000", findings[0].claim)

    def test_a_resumed_run_does_not_add_a_cost_finding(self):
        request = build_resumed_request(
            self.root,
            [("run-1", emit_two_modes()), ("run-2", emit_two_modes(bases=0, resumed=True))],
            make_two_mode_summary(),
        )
        self.assertEqual(self.evaluator.evaluate(request), [])

    def test_the_cost_key_names_the_task_and_its_run_directory(self):
        request = build_request(
            self.root, [("run-1", emit_two_modes(), make_two_mode_summary(models_usd=5.0))]
        )
        findings = self.evaluator.evaluate(request)
        self.assertEqual(len(findings), 1)
        path = next(iter(request.summaries))
        self.assertEqual(findings[0].key, f"rules:cost:task-1:{path}")

    def test_the_same_task_in_two_experiments_stays_two_cost_findings(self):
        runs = [
            ("run-1", emit_two_modes(), make_two_mode_summary(models_usd=5.0)),
            ("run-2", emit_two_modes(), make_two_mode_summary(models_usd=6.0)),
        ]
        request = build_request(self.root, runs)
        findings = [
            finding
            for finding in self.evaluator.evaluate(request)
            if finding.key.startswith("rules:cost:")
        ]
        self.assertEqual(len(findings), 2)
        self.assertNotEqual(findings[0].key, findings[1].key)
        result = evalgraph.run(evalgraph.default_config(["rules"]), request)
        aggregated = [
            finding for finding in result.findings if finding.key.startswith("rules:cost:")
        ]
        self.assertEqual(len(aggregated), 2)

    def test_a_trace_of_only_a_resumed_run_reads_its_last_run(self):
        request = build_resumed_request(
            self.root,
            [("run-1", emit_two_modes(bases=0, resumed=True))],
            make_two_mode_summary(),
        )
        findings = [
            finding for finding in self.evaluator.evaluate(request) if finding.viewpoint == "cost"
        ]
        self.assertEqual(len(findings), 1)
        self.assertIn("8.000000", findings[0].claim)
        self.assertIn("could not be subtracted", findings[0].claim)

    def test_without_a_base_rollout_the_base_cost_is_not_subtracted(self):
        runs = [("run-1", emit_two_modes(bases=0), make_two_mode_summary())]
        findings = [finding for finding in self.findings(runs) if finding.viewpoint == "cost"]
        self.assertEqual(len(findings), 1)
        self.assertIn("8.000000", findings[0].claim)
        self.assertIn("7.000000", findings[0].claim)
        self.assertIn("could not be subtracted", findings[0].claim)


class RetryTests(RulesTestCase):
    def test_switches_at_the_limit_are_quiet(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                for _ in range(3):
                    trace.event(trace.Kind.MODEL_SWITCH, role="improver", to=None)

        self.assertEqual(self.findings([("run-1", body, {})]), [])

    def test_too_many_switches_are_minor(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                for _ in range(4):
                    trace.event(trace.Kind.MODEL_SWITCH, role="improver", to=None)

        findings = self.findings([("run-1", body, {})])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].viewpoint, "reproducibility")
        self.assertEqual(findings[0].severity, Severity.MINOR)

    def test_stage_attempts_at_the_limit_are_quiet(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                with trace.span(trace.Kind.STAGE, label="agg1", mode="agg") as stage:
                    stage.set(attempts=2, error="")

        self.assertEqual(self.findings([("run-1", body, {})]), [])

    def test_stage_attempts_over_the_limit_are_minor(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                with trace.span(trace.Kind.STAGE, label="agg1", mode="agg") as stage:
                    stage.set(attempts=3, error="")

        findings = self.findings([("run-1", body, {})])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].viewpoint, "reproducibility")
        self.assertEqual(findings[0].severity, Severity.MINOR)


def emit_fence(*, succeeded, selection_succeeded=False):
    """A TASK with one fenced candidate and a stage that did or did not succeed."""

    def body():
        with trace.span(trace.Kind.TASK, task_id="task-1"):
            trace.event(trace.Kind.MODEL_FENCE, role="improver", fenced=False, model="m")
            with trace.span(trace.Kind.STAGE, label="agg1", mode="agg") as stage:
                if succeeded:
                    stage.set(attempts=1, kept_base=False, fell_back=False, error="")
                else:
                    stage.set(attempts=0, kept_base=True, fell_back=True, error="no fenced model")
            if selection_succeeded:
                with trace.span(trace.Kind.SELECTION, mode="agg") as selection:
                    selection.set(ok=True, chosen_label="agg1", usd=0.0, error="")

    return body


class FenceTests(RulesTestCase):
    def test_no_success_after_the_fence_is_major(self):
        findings = self.findings([("run-1", emit_fence(succeeded=False), {})])
        safety = [finding for finding in findings if finding.viewpoint == "safety"]
        self.assertEqual(len(safety), 1)
        self.assertEqual(safety[0].severity, Severity.MAJOR)

    def test_a_later_stage_success_is_info(self):
        findings = self.findings([("run-1", emit_fence(succeeded=True), {})])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.INFO)

    def test_a_successful_selection_alone_does_not_count(self):
        body = emit_fence(succeeded=False, selection_succeeded=True)
        findings = self.findings([("run-1", body, {})])
        safety = [finding for finding in findings if finding.viewpoint == "safety"]
        self.assertEqual(len(safety), 1)
        self.assertEqual(safety[0].severity, Severity.MAJOR)

    def test_all_stages_falling_back_is_major(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                with trace.span(trace.Kind.MODE, mode="agg"):
                    for label in ("inv1", "dom1", "agg1"):
                        with trace.span(trace.Kind.STAGE, label=label) as stage:
                            trace.event(
                                trace.Kind.MODEL_FENCE, role="improver", fenced=False, model="m"
                            )
                            stage.set(fell_back=True, attempts=1, error="no fenced runner")
                    with trace.span(trace.Kind.SELECTION, mode="agg") as selection:
                        selection.set(ok=True, chosen_label="agg1", error="")

        findings = self.findings([("run-1", body, {})])
        safety = [finding for finding in findings if finding.viewpoint == "safety"]
        self.assertEqual(len(safety), 1)
        self.assertEqual(safety[0].severity, Severity.MAJOR)

    def test_a_stage_before_the_fence_does_not_count(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                with trace.span(trace.Kind.STAGE, label="agg1", mode="agg") as stage:
                    stage.set(attempts=1, kept_base=False, fell_back=False, error="")
                trace.event(trace.Kind.MODEL_FENCE, role="improver", fenced=False, model="m")

        findings = self.findings([("run-1", body, {})])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.MAJOR)

    def test_a_fence_of_a_stage_that_succeeded_is_recovered(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                with trace.span(trace.Kind.STAGE, label="inv1") as inv:
                    trace.event(trace.Kind.MODEL_FENCE, role="improver", fenced=False, model="m")
                    inv.set(attempts=1, fell_back=False, error="")
                with trace.span(trace.Kind.STAGE, label="dom1") as dom:
                    trace.event(trace.Kind.MODEL_FENCE, role="improver", fenced=False, model="m")
                    dom.set(attempts=1, fell_back=False, error="")
                with trace.span(trace.Kind.STAGE, label="agg1") as agg:
                    agg.set(attempts=1, fell_back=True, error="no fenced runner")

        findings = self.findings([("run-1", body, {})])
        safety = [finding for finding in findings if finding.viewpoint == "safety"]
        self.assertEqual(len(safety), 1)
        self.assertEqual(safety[0].severity, Severity.INFO)

    def test_one_unrecovered_fence_keeps_the_task_major(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                trace.event(trace.Kind.MODEL_FENCE, role="improver", fenced=False, model="m")
                with trace.span(trace.Kind.STAGE, label="inv1") as inv:
                    inv.set(attempts=1, fell_back=False, error="")
                trace.event(trace.Kind.MODEL_FENCE, role="improver", fenced=False, model="m")
                with trace.span(trace.Kind.STAGE, label="agg1") as agg:
                    agg.set(attempts=1, fell_back=True, error="no fenced runner")

        findings = self.findings([("run-1", body, {})])
        safety = [finding for finding in findings if finding.viewpoint == "safety"]
        self.assertEqual(len(safety), 1)
        self.assertEqual(safety[0].severity, Severity.MAJOR)

    def test_no_fence_means_no_finding(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                trace.event(trace.Kind.MODEL_FENCE, role="improver", fenced=True, model="m")

        self.assertEqual(self.findings([("run-1", body, {})]), [])


def emit_error(where, task_id=None):
    def body():
        with trace.span(trace.Kind.TASK, task_id=task_id or f"task-{where}"):
            trace.event(trace.Kind.ERROR, where=where, message="failed")

    return body


class ErrorTests(RulesTestCase):
    def test_one_task_is_info(self):
        findings = self.findings([("run-1", emit_error("base"), {})])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].viewpoint, "reproducibility")
        self.assertEqual(findings[0].severity, Severity.INFO)
        self.assertEqual(findings[0].key, "rules:errors:base")

    def test_two_tasks_are_minor(self):
        findings = self.findings(
            [
                ("run-1", emit_error("base", task_id="task-1"), {}),
                ("run-2", emit_error("base", task_id="task-2"), {}),
            ]
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.MINOR)
        self.assertEqual(len(findings[0].evidence), 2)

    def test_one_task_run_twice_is_info(self):
        findings = self.findings(
            [
                ("run-1", emit_error("base", task_id="task-1"), {}),
                ("run-2", emit_error("base", task_id="task-1"), {}),
            ]
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.INFO)
        self.assertIn("1 task", findings[0].claim)
        self.assertEqual(len(findings[0].evidence), 2)

    def test_a_failed_span_is_counted_by_its_kind(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                try:
                    with trace.span(trace.Kind.SELECTION, mode="agg"):
                        raise RuntimeError("boom")
                except RuntimeError:
                    pass

        findings = self.findings([("run-1", body, {})])
        self.assertEqual([finding.key for finding in findings], ["rules:errors:selection"])
        self.assertEqual(findings[0].severity, Severity.INFO)

    def test_the_evidence_is_capped_at_ten(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                for _ in range(12):
                    trace.event(trace.Kind.ERROR, where="many", message="failed")

        findings = self.findings([("run-1", body, {})])
        self.assertEqual(len(findings[0].evidence), 10)

    def test_a_caught_failure_of_a_rollout_is_counted(self):
        def emit(task_id):
            def body():
                with trace.span(trace.Kind.TASK, task_id=task_id):
                    with trace.span(trace.Kind.ROLLOUT, role="base") as rollout:
                        rollout.set(ok=False, error="executor crashed")

            return body

        findings = self.findings([("run-1", emit("task-1"), {}), ("run-2", emit("task-2"), {})])
        self.assertEqual([finding.key for finding in findings], ["rules:errors:rollout"])
        self.assertEqual(findings[0].severity, Severity.MINOR)
        self.assertEqual(len(findings[0].evidence), 2)

    def test_a_caught_failure_of_a_fallback_stage_is_counted(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                with trace.span(trace.Kind.STAGE, label="agg1") as stage:
                    stage.set(attempts=1, fell_back=True, error="no fenced runner")

        findings = self.findings([("run-1", body, {})])
        self.assertEqual([finding.key for finding in findings], ["rules:errors:stage"])

    def test_a_failed_mode_is_counted_once_through_its_error_event(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1"):
                with trace.span(trace.Kind.MODE, mode="agg") as mode_span:
                    trace.event(trace.Kind.ERROR, where="mode:agg", message="agg: boom")
                    mode_span.set(ok=False, error="agg: boom")

        findings = self.findings([("run-1", body, {})])
        self.assertEqual([finding.key for finding in findings], ["rules:errors:mode:agg"])
        self.assertEqual(len(findings[0].evidence), 1)


class ScaleTests(unittest.TestCase):
    def test_three_hundred_runs_finish_in_seconds(self):
        runs, per_run = 300, 100
        nodes = {}
        for index in range(runs):
            run_id = f"run-{index}"
            source = Path("/synthetic/trace.jsonl")
            nodes[(run_id, "task")] = graph.Node(
                key=(run_id, "task"),
                kind=trace.Kind.TASK,
                run_id=run_id,
                span_id="task",
                parent=None,
                attrs={"task_id": f"task-{index}", "k": 1},
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:00:01+00:00",
                status="ok",
                dur_s=1.0,
                source=source,
            )
            for number in range(per_run):
                span_id = f"r{number:03d}"
                nodes[(run_id, span_id)] = graph.Node(
                    key=(run_id, span_id),
                    kind=trace.Kind.ROLLOUT,
                    run_id=run_id,
                    span_id=span_id,
                    parent=(run_id, "task"),
                    attrs={"role": "base", "ok": True},
                    start_ts="2026-01-01T00:00:02+00:00",
                    end_ts="2026-01-01T00:00:03+00:00",
                    status="ok",
                    dur_s=1.0,
                    source=source,
                )
        request = EvalRequest(
            graph=graph.Graph(nodes=nodes),
            summaries={},
            viewpoints=evaluate.VIEWPOINTS,
        )
        started = time.perf_counter()
        self.assertEqual(RulesEvaluator().evaluate(request), [])
        self.assertLess(time.perf_counter() - started, 5.0)


class FindingShapeTests(RulesTestCase):
    def test_keys_are_unique_and_every_claim_is_a_sentence(self):
        def body():
            with trace.span(trace.Kind.TASK, task_id="task-1", k=1):
                for _ in range(4):
                    trace.event(trace.Kind.MODEL_SWITCH, role="improver", to=None)
                trace.event(trace.Kind.MODEL_FENCE, role="improver", fenced=False, model="m")
                trace.event(trace.Kind.ERROR, where="base", message="failed")
                with trace.span(trace.Kind.MODE, mode="agg"):
                    with trace.span(trace.Kind.ROLLOUT, role="candidate", mode="agg"):
                        pass
                    with trace.span(trace.Kind.STAGE, label="agg1", mode="agg") as stage:
                        stage.set(attempts=3, fell_back=True, error="no fenced model")
                    trace.event(trace.Kind.COST, mode="agg", rollout_usd=0.0)

        summary = make_summary()
        summary["modes"]["agg"]["ledger"]["R"] = 3
        summary["modes"]["agg"]["ledger"]["rollout_usd"] = 0.0
        summary["models"]["executor"][0]["cost_usd"] = 1.0
        findings = self.findings([("run-1", body, summary)])
        self.assertGreater(len(findings), 1)
        keys = [finding.key for finding in findings]
        self.assertEqual(len(keys), len(set(keys)))
        for finding in findings:
            self.assertTrue(finding.key.startswith("rules:"), finding.key)
            self.assertEqual(finding.evaluator, "rules")
            self.assertTrue(finding.suggestion)
            self.assertTrue(finding.claim.endswith("."), finding.claim)


class _ExplodingScores:
    def __getitem__(self, key):
        raise AssertionError("the rules evaluator must not read scores")


class ScoreTests(RulesTestCase):
    def test_scores_are_not_read(self):
        runs = [("run-1", emit_task(), make_summary())]
        self.assertEqual(self.findings(runs, scores=_ExplodingScores()), [])


class RegistrationTests(unittest.TestCase):
    def test_evaluator_for_builds_rules(self):
        evaluator = evaluate.evaluator_for("rules", options={"max_switches": 1})
        self.assertIsInstance(evaluator, RulesEvaluator)
        self.assertEqual(evaluator.max_switches, 1)
        self.assertIn("rules", evaluate.names())

    def test_rules_can_be_imported_before_evaluate(self):
        root = Path(__file__).resolve().parents[1]
        code = (
            "import harness2.evaluators.rules as rules\n"
            "from harness2.core import evaluate\n"
            "evaluator = evaluate.evaluator_for('rules')\n"
            "assert isinstance(evaluator, rules.RulesEvaluator)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class RulesEvaluatorContractTest(EvaluatorContractTests, unittest.TestCase):
    def make_evaluator(self):
        return RulesEvaluator()
