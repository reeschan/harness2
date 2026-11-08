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

"""Tests for `harness2.core.evaluate`: the names, the registry and the finding round trip."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness2.core import evaluate, trace
from harness2.testing.evaluator_contract import FixedEvaluator, contract_request

IMPORT_PATH = "harness2.testing.evaluator_contract:FixedEvaluator"


class FakeEntryPoint:
    def __init__(self, name, target):
        self.name = name
        self._target = target
        self.loaded = 0

    def load(self):
        self.loaded += 1
        return self._target


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(evaluate._REGISTRY)
        self.addCleanup(self._restore)
        evaluate._REGISTRY.clear()
        self.entry_points = []
        self.entry_point_calls = []
        patcher = mock.patch("importlib.metadata.entry_points", self._entry_points)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore(self):
        evaluate._REGISTRY.clear()
        evaluate._REGISTRY.update(self._saved)

    def _entry_points(self, **kwargs):
        self.entry_point_calls.append(kwargs)
        return list(self.entry_points)

    def test_group_name(self):
        self.assertEqual(evaluate.ENTRY_POINT_GROUP, "harness2.evaluators")

    def test_entry_points_asks_for_the_group(self):
        evaluate._entry_points()
        self.assertEqual(self.entry_point_calls, [{"group": evaluate.ENTRY_POINT_GROUP}])

    def test_register_and_resolve(self):
        def factory(**options):
            return FixedEvaluator()

        evaluate.register("mine", factory)
        self.assertIs(evaluate.resolve_factory("mine"), factory)

    def test_registration_replaces(self):
        def first(**options):
            return FixedEvaluator()

        def second(**options):
            return FixedEvaluator()

        evaluate.register("mine", first)
        evaluate.register("mine", second)
        self.assertIs(evaluate.resolve_factory("mine"), second)

    def test_resolve_by_entry_point_name(self):
        def factory(**options):
            return FixedEvaluator()

        self.entry_points.append(FakeEntryPoint("ext", factory))
        self.assertIs(evaluate.resolve_factory("ext"), factory)

    def test_builtin_registration_comes_first(self):
        def factory(**options):
            return FixedEvaluator()

        evaluate.register("ext", factory)
        ep = FakeEntryPoint("ext", object())
        self.entry_points.append(ep)
        self.assertIs(evaluate.resolve_factory("ext"), factory)
        self.assertEqual(ep.loaded, 0)
        self.assertEqual(evaluate.names().count("ext"), 1)

    def test_entry_point_comes_before_import_path(self):
        def factory(**options):
            return FixedEvaluator()

        self.entry_points.append(FakeEntryPoint(IMPORT_PATH, factory))
        self.assertIs(evaluate.resolve_factory(IMPORT_PATH), factory)

    def test_import_path_still_works(self):
        self.assertIs(evaluate.resolve_factory(IMPORT_PATH), FixedEvaluator)

    def test_import_path_errors(self):
        with self.assertRaises(ValueError):
            evaluate.resolve_factory("harness2.core.evaluate:")
        with self.assertRaises(ValueError):
            evaluate.resolve_factory("missing.module:Thing")
        with self.assertRaises(ValueError):
            evaluate.resolve_factory("harness2.core.evaluate:missing")
        with self.assertRaises(TypeError):
            evaluate.resolve_factory("harness2.core.trace:MAX_ATTR_CHARS")

    def test_unknown_name_lists_registered_and_entry_points(self):
        def factory(**options):
            return FixedEvaluator()

        evaluate.register("mine", factory)
        self.entry_points.append(FakeEntryPoint("ext", factory))
        with self.assertRaises(ValueError) as caught:
            evaluate.resolve_factory("nope")
        message = str(caught.exception)
        self.assertIn("unknown evaluator 'nope'", message)
        self.assertIn("mine", message)
        self.assertIn("ext", message)
        self.assertIn(evaluate.ENTRY_POINT_GROUP, message)

    def test_broken_entry_point_load_raises_value_error(self):
        class Broken:
            name = "broken"

            def load(self):
                raise ImportError("no module")

        self.entry_points.append(Broken())
        with self.assertRaises(ValueError) as caught:
            evaluate.resolve_factory("broken")
        message = str(caught.exception)
        self.assertIn("broken", message)
        self.assertIn(evaluate.ENTRY_POINT_GROUP, message)

    def test_uncallable_entry_point_raises_type_error(self):
        self.entry_points.append(FakeEntryPoint("number", 42))
        with self.assertRaises(TypeError) as caught:
            evaluate.resolve_factory("number")
        self.assertIn("number", str(caught.exception))

    def test_names_include_entry_points(self):
        def factory(**options):
            return FixedEvaluator()

        evaluate.register("mine", factory)
        self.entry_points.extend(
            [
                FakeEntryPoint("b-ext", factory),
                FakeEntryPoint("a-ext", factory),
            ]
        )
        found = evaluate.names()
        self.assertIn("a-ext", found)
        self.assertIn("b-ext", found)
        self.assertIn("mine", found)
        self.assertEqual(found, sorted(set(found)))

    def test_evaluator_for_calls_the_factory_with_options(self):
        calls = []

        def factory(**options):
            calls.append(options)
            return FixedEvaluator()

        evaluate.register("mine", factory)
        built = evaluate.evaluator_for("mine", options={"max_switches": 2})
        self.assertIsInstance(built, FixedEvaluator)
        self.assertEqual(calls, [{"max_switches": 2}])
        evaluate.evaluator_for("mine")
        self.assertEqual(calls[-1], {})

    def test_evaluator_for_rejects_a_non_mapping_options(self):
        def factory(**options):
            return FixedEvaluator()

        evaluate.register("mine", factory)
        with self.assertRaises(TypeError) as caught:
            evaluate.evaluator_for("mine", options=[("max_switches", 2)])
        self.assertIn("options", str(caught.exception))

    def test_evaluator_for_requires_an_evaluator(self):
        def factory(**options):
            return object()

        evaluate.register("bad", factory)
        with self.assertRaises(TypeError) as caught:
            evaluate.evaluator_for("bad")
        message = str(caught.exception)
        self.assertIn("bad", message)
        self.assertIn("Evaluator", message)

    def test_evaluator_for_builds_an_import_path(self):
        self.assertIsInstance(evaluate.evaluator_for(IMPORT_PATH), FixedEvaluator)


class SeverityTests(unittest.TestCase):
    def test_rank_orders_heaviest_first(self):
        self.assertEqual(
            [
                evaluate.Severity.rank(item)
                for item in (
                    evaluate.Severity.CRITICAL,
                    evaluate.Severity.MAJOR,
                    evaluate.Severity.MINOR,
                    evaluate.Severity.INFO,
                )
            ],
            [0, 1, 2, 3],
        )

    def test_all_lists_the_constants(self):
        self.assertEqual(evaluate.Severity.ALL, ("critical", "major", "minor", "info"))

    def test_unknown_rank_raises_value_error(self):
        with self.assertRaises(ValueError):
            evaluate.Severity.rank("fatal")


class ViewpointTests(unittest.TestCase):
    def test_viewpoints_are_the_six_named_ones(self):
        self.assertEqual(
            [item.name for item in evaluate.VIEWPOINTS],
            ["cost", "budget", "effect", "safety", "reproducibility", "generalization"],
        )

    def test_reads_are_trace_kinds(self):
        kinds = {
            value
            for name, value in vars(trace.Kind).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        for item in evaluate.VIEWPOINTS:
            self.assertTrue(item.reads, f"{item.name} must declare the kinds it reads")
            for kind in item.reads:
                self.assertIn(kind, kinds, f"{item.name} reads an unknown kind {kind!r}")

    def test_descriptions_are_one_sentence(self):
        for item in evaluate.VIEWPOINTS:
            self.assertTrue(item.description.endswith("."), item.name)
            self.assertNotIn("\n", item.description, item.name)

    def test_viewpoint_looks_up_by_name(self):
        self.assertEqual(evaluate.viewpoint("cost").name, "cost")
        with self.assertRaises(ValueError):
            evaluate.viewpoint("quality")


class FindingTests(unittest.TestCase):
    def finding(self, **kwargs):
        values = {
            "viewpoint": "cost",
            "severity": evaluate.Severity.MAJOR,
            "claim": "the run spent three times the budget",
        }
        values.update(kwargs)
        return evaluate.Finding(**values)

    def test_round_trip_through_json(self):
        evidence = (
            evaluate.Evidence("r1", "s1", "the cost event"),
            evaluate.Evidence("r1", "s2"),
        )
        original = self.finding(
            evidence=evidence, suggestion="spend less", key="cost", evaluator="rules"
        )
        loaded = evaluate.Finding.from_dict(json.loads(json.dumps(original.to_dict())))
        self.assertEqual(loaded, original)
        self.assertEqual(loaded.evidence, evidence)

    def test_to_dict_has_the_seven_fields(self):
        data = self.finding(evidence=(evaluate.Evidence("r1", "s1"),)).to_dict()
        self.assertEqual(
            sorted(data),
            ["claim", "evaluator", "evidence", "key", "severity", "suggestion", "viewpoint"],
        )
        self.assertEqual(data["evidence"], [{"run_id": "r1", "span_id": "s1", "note": ""}])
        self.assertEqual(data["key"], "")

    def test_from_dict_ignores_unknown_keys(self):
        data = self.finding().to_dict()
        data["viewpoints"] = ["cost", "budget"]
        data["evaluators"] = ["rules"]
        self.assertEqual(evaluate.Finding.from_dict(data), self.finding())

    def test_dedupe_key_uses_the_key_when_set(self):
        self.assertEqual(self.finding(key="budget:rollout").dedupe_key(), "budget:rollout")

    def test_dedupe_key_falls_back_to_viewpoint_and_claim(self):
        self.assertEqual(
            self.finding().dedupe_key(),
            "cost:the run spent three times the budget",
        )

    def test_a_list_of_evidence_becomes_a_tuple(self):
        evidence = evaluate.Evidence("r1", "s1")
        found = self.finding(evidence=[evidence])
        self.assertEqual(found.evidence, (evidence,))
        self.assertIsInstance(found.evidence, tuple)

    def test_unknown_severity_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.finding(severity="fatal")

    def test_evidence_that_is_not_evidence_raises_type_error(self):
        with self.assertRaises(TypeError):
            self.finding(evidence=("r1/s1",))
        with self.assertRaises(TypeError):
            self.finding(evidence="r1/s1")

    def test_findings_are_hashable(self):
        found = self.finding(evidence=(evaluate.Evidence("r1", "s1"),))
        self.assertEqual(len({found, found}), 1)

    def test_evidence_from_dict_requires_both_ids(self):
        self.assertEqual(
            evaluate.Evidence.from_dict({"run_id": "r1", "span_id": "s1"}),
            evaluate.Evidence("r1", "s1"),
        )
        with self.assertRaises(ValueError):
            evaluate.Evidence.from_dict({"run_id": "r1"})
        with self.assertRaises(ValueError):
            evaluate.Evidence.from_dict({"span_id": "s1"})


class EvalRequestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_summary_for_reads_the_summary_next_to_the_trace(self):
        request = contract_request(self.root)
        self.assertEqual(request.summary_for("contract"), {"task_id": "contract"})
        self.assertIsNone(request.summary_for("missing"))

    def test_scores_default_to_none(self):
        self.assertIsNone(contract_request(self.root).scores)

    def test_options_default_to_a_fresh_dict(self):
        first = contract_request(self.root)
        second = contract_request(self.root)
        self.assertEqual(first.options, {})
        self.assertIsNot(first.options, second.options)


if __name__ == "__main__":
    unittest.main()
