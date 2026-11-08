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

"""Tests for `harness2.testing.evaluator_contract`: the fake evaluator and the checks."""

import tempfile
import unittest
from pathlib import Path

from harness2.core import evaluate, trace
from harness2.core.evaluate import Evidence, Finding, Severity, Viewpoint
from harness2.testing.evaluator_contract import (
    EvaluatorContractTests,
    FixedEvaluator,
    check_evaluator,
    contract_request,
)


class ContractRequestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_request_has_one_task_with_two_children(self):
        request = contract_request(self.root)
        roots = request.graph.roots()
        self.assertEqual(len(roots), 1)
        children = request.graph.children(roots[0].key)
        self.assertEqual(
            sorted(child.kind for child in children),
            sorted([trace.Kind.ROLLOUT, trace.Kind.STAGE]),
        )

    def test_request_records_scores_and_viewpoints(self):
        request = contract_request(self.root, scores={"contract": [{"score": 1}]})
        self.assertEqual(request.scores, {"contract": [{"score": 1}]})
        self.assertEqual(request.viewpoints, evaluate.VIEWPOINTS)
        self.assertEqual(request.summary_for("contract"), {"task_id": "contract"})


class FixedEvaluatorTests(unittest.TestCase):
    def test_viewpoints_default_to_all_six_without_findings(self):
        self.assertEqual(
            [item.name for item in FixedEvaluator().viewpoints()],
            [item.name for item in evaluate.VIEWPOINTS],
        )

    def test_viewpoints_follow_the_findings(self):
        found = Finding("safety", Severity.MAJOR, "a candidate was fenced")
        self.assertEqual([item.name for item in FixedEvaluator([found]).viewpoints()], ["safety"])

    def test_declared_viewpoints_win(self):
        item = Viewpoint("quality", "How good the change is.")
        self.assertEqual(FixedEvaluator([], [item]).viewpoints(), [item])

    def test_evaluate_returns_a_new_list(self):
        found = Finding("cost", Severity.MINOR, "the cost grew")
        evaluator = FixedEvaluator([found])
        first = evaluator.evaluate(None)
        second = evaluator.evaluate(None)
        self.assertEqual(first, [found])
        self.assertIsNot(first, second)


class FixedEvaluatorContractTest(EvaluatorContractTests, unittest.TestCase):
    def make_evaluator(self):
        task = self.request.graph.roots()[0]
        child = self.request.graph.children(task.key)[0]
        found = Finding(
            viewpoint="cost",
            severity=Severity.MINOR,
            claim="a child span ran",
            evidence=(Evidence(task.run_id, child.span_id, "the child"),),
        )
        return FixedEvaluator([found])


class ContractFailureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.request = contract_request(Path(self._tmp.name))

    def _evidence(self):
        task = self.request.graph.roots()[0]
        return Evidence(task.run_id, task.span_id, "the task")

    def test_missing_evidence_fails(self):
        found = Finding(
            "cost",
            Severity.MAJOR,
            "the cost grew",
            evidence=(Evidence("contract", "missing"),),
        )
        with self.assertRaises(AssertionError) as caught:
            check_evaluator(FixedEvaluator([found]), self.request)
        self.assertIn("missing", str(caught.exception))

    def test_empty_viewpoints_fail(self):
        with self.assertRaises(AssertionError) as caught:
            check_evaluator(FixedEvaluator([], viewpoints=[]))
        self.assertIn("viewpoints", str(caught.exception))

    def test_a_viewpoint_of_another_evaluator_fails(self):
        found = Finding(
            "safety", Severity.MAJOR, "a candidate was fenced", evidence=(self._evidence(),)
        )
        evaluator = FixedEvaluator([found], viewpoints=[evaluate.viewpoint("cost")])
        with self.assertRaises(AssertionError) as caught:
            check_evaluator(evaluator, self.request)
        self.assertIn("safety", str(caught.exception))

    def test_a_viewpoint_name_that_is_not_a_file_name_fails(self):
        evaluator = FixedEvaluator([], viewpoints=[Viewpoint("Cost-1", "A bad name.")])
        with self.assertRaises(AssertionError) as caught:
            check_evaluator(evaluator)
        self.assertIn("Cost-1", str(caught.exception))

    def test_a_non_finding_result_fails(self):
        class NotFindings:
            def viewpoints(self):
                return list(evaluate.VIEWPOINTS)

            def evaluate(self, request):
                return ["nope"]

        with self.assertRaises(AssertionError) as caught:
            check_evaluator(NotFindings(), self.request)
        self.assertIn("Finding", str(caught.exception))

    def test_a_result_that_is_not_a_list_fails(self):
        class TupleFindings:
            def viewpoints(self):
                return list(evaluate.VIEWPOINTS)

            def evaluate(self, request):
                return ()

        with self.assertRaises(AssertionError) as caught:
            check_evaluator(TupleFindings(), self.request)
        self.assertIn("list", str(caught.exception))

    def test_an_evaluator_that_needs_scores_not_to_be_empty_fails(self):
        class NoScores:
            def viewpoints(self):
                return list(evaluate.VIEWPOINTS)

            def evaluate(self, request):
                if request.scores is not None:
                    raise AssertionError("scores must be None")
                return []

        with self.assertRaises(AssertionError) as caught:
            check_evaluator(NoScores(), self.request)
        self.assertIn("scores must be None", str(caught.exception))

    def test_checking_without_a_request_is_static(self):
        found = Finding(
            "cost",
            Severity.MAJOR,
            "the cost grew",
            evidence=(Evidence("contract", "missing"),),
        )
        self.assertEqual(check_evaluator(FixedEvaluator([found])), [])

    def test_an_object_that_is_not_an_evaluator_fails(self):
        with self.assertRaises(AssertionError) as caught:
            check_evaluator(object())
        self.assertIn("Evaluator", str(caught.exception))

    def test_the_mixin_is_not_a_test_case(self):
        self.assertFalse(issubclass(EvaluatorContractTests, unittest.TestCase))


if __name__ == "__main__":
    unittest.main()
