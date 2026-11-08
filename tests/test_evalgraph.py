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

"""Tests for `harness2.core.evalgraph`: the config, the rebutters, the run and the merge."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from harness2.core import evalgraph, evaluate, graph, trace
from harness2.core.evaluate import EvalRequest, Evidence, Finding, Severity, Viewpoint
from harness2.core.trace_file import FILE_NAME, FileSink
from harness2.testing.evaluator_contract import FixedEvaluator, contract_request


class FixedRebutter:
    """A rebutter that returns the verdicts it was given."""

    def __init__(self, verdicts=()):
        self._verdicts = tuple(verdicts)

    def rebut(self, findings, request):
        return list(self._verdicts)


class RecordingRebutter:
    """A rebutter that records how many findings it was given."""

    def __init__(self):
        self.counts = []

    def rebut(self, findings, request):
        self.counts.append(len(findings))
        return []


class BoomEvaluator:
    """An evaluator that raises once it runs."""

    def viewpoints(self):
        return [evaluate.viewpoint("cost")]

    def evaluate(self, request):
        raise RuntimeError("boom")


class NonListEvaluator:
    """An evaluator that returns something other than a list."""

    def viewpoints(self):
        return [evaluate.viewpoint("cost")]

    def evaluate(self, request):
        return None


class RecordingEvaluator:
    """An evaluator that records the requests it sees."""

    def __init__(self, span_id=None):
        self.span_id = span_id
        self.requests = []
        self.seen = []

    def viewpoints(self):
        return [evaluate.viewpoint("cost")]

    def evaluate(self, request):
        self.requests.append(request)
        if self.span_id is not None:
            self.seen.append(request.graph.find("r1", self.span_id))
        return []


class EvalGraphCase(unittest.TestCase):
    """A contract request, a clean evaluator registry and an empty entry point list."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.request = contract_request(self.root)
        self.task = self.request.graph.roots()[0]
        self._evaluators = dict(evaluate._REGISTRY)
        self._rebutters = dict(evalgraph._REBUTTERS)
        self.addCleanup(self._restore)
        evaluate._REGISTRY.clear()
        evalgraph._REBUTTERS.clear()
        patcher = mock.patch("importlib.metadata.entry_points", lambda **kwargs: [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore(self):
        evaluate._REGISTRY.clear()
        evaluate._REGISTRY.update(self._evaluators)
        evalgraph._REBUTTERS.clear()
        evalgraph._REBUTTERS.update(self._rebutters)

    def finding(self, *, viewpoint="cost", severity=Severity.MAJOR, claim="claim", key="key"):
        evidence = (Evidence(self.task.run_id, self.task.span_id, "task"),)
        return Finding(
            viewpoint=viewpoint,
            severity=severity,
            claim=claim,
            evidence=evidence,
            key=key,
        )

    def register(self, name, findings=(), viewpoints=None):
        evaluator = FixedEvaluator(findings, viewpoints)
        evaluate.register(name, lambda **options: evaluator)
        return evaluator

    def register_rebutter(self, name, verdicts=()):
        rebutter = FixedRebutter(verdicts)
        evalgraph.register_rebutter(name, lambda **options: rebutter)
        return rebutter

    def config(self, evaluators, *, edges=None, allow_scores=False):
        nodes = [{"id": name, "type": "evaluator", "evaluator": name} for name in evaluators]
        nodes.append({"id": "aggregate", "type": "aggregate"})
        if edges is None:
            edges = [[name, "aggregate"] for name in evaluators]
        return {"nodes": nodes, "edges": edges, "allow_scores": allow_scores}

    def rebut_config(self, evaluator="alpha", rebutter="deny"):
        return {
            "nodes": [
                {"id": evaluator, "type": "evaluator", "evaluator": evaluator},
                {"id": "rebut", "type": "rebut", "rebutter": rebutter},
                {"id": "aggregate", "type": "aggregate"},
            ],
            "edges": [[evaluator, "rebut"], ["rebut", "aggregate"]],
        }


class DefaultConfigTests(EvalGraphCase):
    def test_one_node_per_evaluator_and_one_aggregate(self):
        config = evalgraph.default_config(["alpha", "beta"])
        self.assertEqual([node["id"] for node in config["nodes"]], ["alpha", "beta", "aggregate"])
        self.assertEqual(config["nodes"][-1]["type"], "aggregate")
        self.assertEqual(config["edges"], [["alpha", "aggregate"], ["beta", "aggregate"]])
        self.assertFalse(config["allow_scores"])

    def test_allow_scores_keyword(self):
        self.assertTrue(evalgraph.default_config(["alpha"], allow_scores=True)["allow_scores"])

    def test_the_default_config_runs(self):
        self.register("alpha", [self.finding(key="a")])
        self.register("beta", [self.finding(key="b")])
        result = evalgraph.run(evalgraph.default_config(["alpha", "beta"]), self.request)
        self.assertEqual(result.errors, [])
        self.assertEqual([finding.key for finding in result.findings], ["a", "b"])

    def test_the_aggregate_id_moves_out_of_the_way_of_an_evaluator(self):
        config = evalgraph.default_config(["aggregate", "beta"])
        self.assertEqual(
            [node["id"] for node in config["nodes"]],
            ["aggregate", "beta", "aggregate_1"],
        )
        self.assertEqual(config["nodes"][-1]["type"], "aggregate")
        self.assertEqual(
            config["edges"],
            [["aggregate", "aggregate_1"], ["beta", "aggregate_1"]],
        )

    def test_the_aggregate_id_keeps_moving_past_a_taken_suffix(self):
        config = evalgraph.default_config(["aggregate", "aggregate_1"])
        self.assertEqual(config["nodes"][-1]["id"], "aggregate_2")
        self.assertEqual(config["edges"][-1], ["aggregate_1", "aggregate_2"])

    def test_an_evaluator_named_aggregate_runs(self):
        self.register("aggregate", [self.finding(key="a")])
        result = evalgraph.run(evalgraph.default_config(["aggregate"]), self.request)
        self.assertEqual(result.errors, [])
        self.assertEqual([finding.key for finding in result.findings], ["a"])


class LoadConfigTests(EvalGraphCase):
    def test_reads_and_checks_a_json_file(self):
        self.register("alpha")
        path = self.root / "graph.json"
        path.write_text(json.dumps(self.config(["alpha"])), encoding="utf-8")
        config = evalgraph.load_config(path)
        self.assertEqual([node["id"] for node in config["nodes"]], ["alpha", "aggregate"])

    def test_bad_json_raises_value_error(self):
        path = self.root / "graph.json"
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            evalgraph.load_config(path)

    def test_missing_file_raises_value_error(self):
        with self.assertRaises(ValueError):
            evalgraph.load_config(self.root / "missing.json")

    def test_a_bad_config_raises_value_error(self):
        path = self.root / "graph.json"
        path.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
        with self.assertRaises(ValueError):
            evalgraph.load_config(path)


class CheckConfigTests(EvalGraphCase):
    def test_a_valid_config_passes(self):
        self.register("alpha")
        evalgraph.check_config(self.config(["alpha"]))

    def test_a_rebut_node_is_allowed(self):
        self.register("alpha")
        evalgraph.register_rebutter("fake", FixedRebutter)
        evalgraph.check_config(self.rebut_config(rebutter="fake"))

    def test_no_aggregate_is_an_error(self):
        self.register("alpha")
        config = {
            "nodes": [{"id": "alpha", "type": "evaluator", "evaluator": "alpha"}],
            "edges": [],
        }
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("aggregate", str(caught.exception))

    def test_two_aggregates_are_an_error(self):
        self.register("alpha")
        config = {
            "nodes": [
                {"id": "alpha", "type": "evaluator", "evaluator": "alpha"},
                {"id": "one", "type": "aggregate"},
                {"id": "two", "type": "aggregate"},
            ],
            "edges": [["alpha", "one"], ["alpha", "two"]],
        }
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("exactly one aggregate", str(caught.exception))

    def test_a_cycle_is_an_error(self):
        config = {
            "nodes": [
                {"id": "alpha", "type": "evaluator", "evaluator": "alpha"},
                {"id": "one", "type": "rebut", "rebutter": "fake"},
                {"id": "two", "type": "rebut", "rebutter": "fake"},
                {"id": "aggregate", "type": "aggregate"},
            ],
            "edges": [
                ["alpha", "one"],
                ["one", "two"],
                ["two", "one"],
                ["two", "aggregate"],
            ],
        }
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("DAG", str(caught.exception))

    def test_an_unknown_type_is_an_error(self):
        config = {"nodes": [{"id": "x", "type": "judge"}], "edges": []}
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("unknown type", str(caught.exception))

    def test_an_unknown_viewpoint_is_an_error(self):
        self.register("alpha")
        config = self.config(["alpha"])
        config["nodes"][0]["viewpoints"] = ["quality"]
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("quality", str(caught.exception))

    def test_a_viewpoint_name_must_be_an_identifier(self):
        self.register("alpha")
        config = self.config(["alpha"])
        config["nodes"][0]["viewpoints"] = ["Cost"]
        with self.assertRaises(ValueError):
            evalgraph.check_config(config)

    def test_an_unknown_evaluator_is_an_error(self):
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(self.config(["missing"]))
        self.assertIn("unknown evaluator 'missing'", str(caught.exception))

    def test_an_unknown_rebutter_is_an_error(self):
        self.register("alpha")
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(self.rebut_config())
        self.assertIn("unknown rebutter 'deny'", str(caught.exception))

    def test_an_unknown_node_key_is_an_error(self):
        self.register("alpha")
        config = self.config(["alpha"])
        config["nodes"][0]["viewpoint"] = ["cost"]
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("unknown keys", str(caught.exception))

    def test_a_duplicate_node_id_is_an_error(self):
        config = {
            "nodes": [
                {"id": "alpha", "type": "evaluator", "evaluator": "alpha"},
                {"id": "alpha", "type": "aggregate"},
            ],
            "edges": [],
        }
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("used twice", str(caught.exception))

    def test_an_evaluator_with_an_incoming_edge_is_an_error(self):
        self.register("alpha")
        self.register("beta")
        config = self.config(["alpha", "beta"], edges=[["alpha", "beta"], ["beta", "aggregate"]])
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("incoming", str(caught.exception))

    def test_a_rebut_node_needs_an_incoming_edge(self):
        evalgraph.register_rebutter("fake", FixedRebutter)
        config = {
            "nodes": [
                {"id": "one", "type": "rebut", "rebutter": "fake"},
                {"id": "aggregate", "type": "aggregate"},
            ],
            "edges": [["one", "aggregate"]],
        }
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("incoming", str(caught.exception))

    def test_the_aggregate_must_not_have_an_outgoing_edge(self):
        self.register("alpha")
        config = self.config(["alpha"])
        config["nodes"].append({"id": "tail", "type": "rebut", "rebutter": "fake"})
        config["edges"] = [["alpha", "aggregate"], ["aggregate", "tail"]]
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("outgoing", str(caught.exception))

    def test_every_node_must_reach_the_aggregate(self):
        self.register("alpha")
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(self.config(["alpha"], edges=[]))
        self.assertIn("does not reach", str(caught.exception))

    def test_an_edge_to_an_unknown_node_is_an_error(self):
        self.register("alpha")
        config = self.config(["alpha"], edges=[["alpha", "nope"]])
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("unknown node", str(caught.exception))

    def test_a_self_loop_is_an_error(self):
        self.register("alpha")
        config = self.config(["alpha"], edges=[["alpha", "alpha"], ["alpha", "aggregate"]])
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("loops", str(caught.exception))

    def test_a_repeated_edge_is_an_error(self):
        self.register("alpha")
        config = self.config(["alpha"], edges=[["alpha", "aggregate"], ["alpha", "aggregate"]])
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("repeats", str(caught.exception))

    def test_allow_scores_must_be_a_bool(self):
        self.register("alpha")
        config = self.config(["alpha"])
        config["allow_scores"] = "yes"
        with self.assertRaises(ValueError):
            evalgraph.check_config(config)

    def test_unknown_top_level_keys_are_an_error(self):
        self.register("alpha")
        config = self.config(["alpha"])
        config["allow_scores_extra"] = True
        with self.assertRaises(ValueError) as caught:
            evalgraph.check_config(config)
        self.assertIn("unknown keys", str(caught.exception))


class VerdictTests(unittest.TestCase):
    def test_stances(self):
        for stance in evalgraph.STANCES:
            self.assertEqual(evalgraph.Verdict("k", stance).stance, stance)

    def test_an_unknown_stance_raises_value_error(self):
        with self.assertRaises(ValueError):
            evalgraph.Verdict("k", "maybe")


class RebutterRegistryTests(EvalGraphCase):
    def test_group_name(self):
        self.assertEqual(evalgraph.REBUTTER_GROUP, "harness2.rebutters")

    def test_register_and_resolve(self):
        def factory(**options):
            return FixedRebutter()

        evalgraph.register_rebutter("mine", factory)
        self.assertIs(evalgraph.resolve_rebutter_factory("mine"), factory)

    def test_registration_replaces(self):
        def first(**options):
            return FixedRebutter()

        def second(**options):
            return FixedRebutter()

        evalgraph.register_rebutter("mine", first)
        evalgraph.register_rebutter("mine", second)
        self.assertIs(evalgraph.resolve_rebutter_factory("mine"), second)

    def test_names_are_sorted(self):
        evalgraph.register_rebutter("b", FixedRebutter)
        evalgraph.register_rebutter("a", FixedRebutter)
        self.assertEqual(evalgraph.rebutter_names(), ["a", "b"])

    def test_rebutter_for_calls_the_factory_with_options(self):
        calls = []

        def factory(**options):
            calls.append(options)
            return FixedRebutter()

        evalgraph.register_rebutter("mine", factory)
        built = evalgraph.rebutter_for("mine", options={"limit": 2})
        self.assertIsInstance(built, FixedRebutter)
        self.assertEqual(calls, [{"limit": 2}])

    def test_an_unknown_name_raises_value_error(self):
        with self.assertRaises(ValueError) as caught:
            evalgraph.rebutter_for("nope")
        self.assertIn("unknown rebutter", str(caught.exception))

    def test_a_factory_that_returns_a_non_rebutter_raises_type_error(self):
        evalgraph.register_rebutter("bad", lambda **options: object())
        with self.assertRaises(TypeError):
            evalgraph.rebutter_for("bad")


class AggregatedFindingTests(unittest.TestCase):
    def test_to_dict_has_the_finding_keys_plus_two(self):
        finding = evalgraph.AggregatedFinding(
            viewpoint="cost",
            severity=Severity.MAJOR,
            claim="claim",
            evidence=(Evidence("r", "s"),),
            key="k",
            viewpoints=("cost", "budget"),
            evaluators=("a",),
        )
        self.assertEqual(
            list(finding.to_dict()),
            [
                "viewpoint",
                "severity",
                "claim",
                "evidence",
                "suggestion",
                "key",
                "evaluator",
                "viewpoints",
                "evaluators",
            ],
        )

    def test_round_trip_through_json(self):
        original = evalgraph.AggregatedFinding(
            viewpoint="cost",
            severity=Severity.MINOR,
            claim="claim",
            evidence=(Evidence("r", "s", "note"),),
            suggestion="fix it",
            key="k",
            evaluator="a",
            viewpoints=("cost", "budget"),
            evaluators=("a", "b"),
        )
        loaded = evalgraph.AggregatedFinding.from_dict(json.loads(json.dumps(original.to_dict())))
        self.assertEqual(loaded, original)

    def test_from_dict_defaults_the_viewpoints(self):
        data = Finding(viewpoint="cost", severity=Severity.MAJOR, claim="claim").to_dict()
        loaded = evalgraph.AggregatedFinding.from_dict(data)
        self.assertEqual(loaded.viewpoints, ("cost",))
        self.assertEqual(loaded.evaluators, ())

    def test_result_to_dict(self):
        result = evalgraph.EvalResult(findings=[], dropped={"no_evidence": 1, "denied": 0})
        self.assertEqual(
            result.to_dict(),
            {"findings": [], "dropped": {"no_evidence": 1, "denied": 0}, "errors": []},
        )


class RunTests(EvalGraphCase):
    def test_a_finding_reaches_the_aggregate(self):
        self.register("alpha", [self.finding(key="a")])
        result = evalgraph.run(self.config(["alpha"]), self.request)
        self.assertEqual(result.errors, [])
        self.assertEqual([finding.key for finding in result.findings], ["a"])
        merged = result.findings[0]
        self.assertIsInstance(merged, evalgraph.AggregatedFinding)
        self.assertEqual(merged.viewpoints, ("cost",))
        self.assertEqual(merged.evaluator, "alpha")
        self.assertEqual([finding.key for finding in result.by_viewpoint["cost"]], ["a"])

    def test_node_viewpoints_filter_the_findings(self):
        findings = [
            self.finding(viewpoint="cost", key="c"),
            self.finding(viewpoint="budget", key="b"),
        ]
        self.register("alpha", findings, [evaluate.viewpoint("cost"), evaluate.viewpoint("budget")])
        config = self.config(["alpha"])
        config["nodes"][0]["viewpoints"] = ["cost"]
        result = evalgraph.run(config, self.request)
        self.assertEqual([finding.key for finding in result.findings], ["c"])
        self.assertEqual(list(result.by_viewpoint), ["cost"])

    def test_a_chosen_viewpoint_without_findings_has_an_empty_list(self):
        self.register(
            "alpha",
            [self.finding(key="c")],
            [evaluate.viewpoint("cost"), evaluate.viewpoint("budget")],
        )
        config = self.config(["alpha"])
        config["nodes"][0]["viewpoints"] = ["cost", "budget"]
        result = evalgraph.run(config, self.request)
        self.assertEqual(list(result.by_viewpoint), ["cost", "budget"])
        self.assertEqual(result.by_viewpoint["budget"], [])

    def test_a_finding_whose_evidence_is_missing_is_dropped(self):
        config = self.config(["alpha"])
        config["nodes"][0]["viewpoints"] = ["cost"]
        finding = Finding(
            viewpoint="cost",
            severity=Severity.MAJOR,
            claim="claim",
            evidence=(Evidence("r", "missing"),),
            key="gone",
        )
        self.register("alpha", [finding])
        result = evalgraph.run(config, self.request)
        self.assertEqual(result.findings, [])
        self.assertEqual(result.dropped["no_evidence"], 1)
        self.assertEqual(len(result.by_viewpoint["cost"]), 1)

    def test_evidence_that_is_not_in_the_graph_is_cut(self):
        good = Evidence(self.task.run_id, self.task.span_id, "here")
        bad = Evidence("r", "missing", "gone")
        self.register("alpha", [self.finding(key="a")])
        config = self.config(["alpha"])
        finding = Finding(
            viewpoint="cost",
            severity=Severity.MAJOR,
            claim="claim",
            evidence=(bad, good),
            key="a",
        )
        self.register("alpha", [finding])
        result = evalgraph.run(config, self.request)
        self.assertEqual(result.findings[0].evidence, (good,))
        self.assertEqual(result.dropped["no_evidence"], 0)

    def test_evidence_ids_that_are_not_strings_are_errors(self):
        self.register(
            "bad",
            [
                Finding(
                    viewpoint="cost",
                    severity=Severity.MAJOR,
                    claim="run",
                    evidence=(Evidence(["x"], "y"),),
                    key="run",
                ),
                Finding(
                    viewpoint="cost",
                    severity=Severity.MAJOR,
                    claim="span",
                    evidence=(Evidence("x", ["y"]),),
                    key="span",
                ),
            ],
        )
        self.register("good", [self.finding(key="good")])
        result = evalgraph.run(self.config(["bad", "good"]), self.request)
        self.assertEqual(len(result.errors), 2)
        for error in result.errors:
            self.assertIn("evidence", error)
            self.assertIn("str", error)
        self.assertEqual([finding.key for finding in result.findings], ["good"])

    def test_deny_drops_the_finding(self):
        self.register("alpha", [self.finding(key="k")])
        self.register_rebutter("deny", [evalgraph.Verdict("k", "deny")])
        result = evalgraph.run(self.rebut_config(), self.request)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.findings, [])
        self.assertEqual(result.dropped["denied"], 1)
        self.assertEqual(len(result.by_viewpoint["cost"]), 1)

    def test_support_and_hold_keep_the_finding(self):
        self.register("alpha", [self.finding(key="k")])
        self.register_rebutter(
            "deny", [evalgraph.Verdict("k", "support"), evalgraph.Verdict("k", "hold")]
        )
        result = evalgraph.run(self.rebut_config(), self.request)
        self.assertEqual([finding.key for finding in result.findings], ["k"])
        self.assertEqual(result.dropped["denied"], 0)

    def test_the_same_finding_from_two_paths_is_counted_once(self):
        self.register("alpha", [self.finding(key="k")])
        self.register_rebutter("deny", [])
        config = self.rebut_config()
        config["edges"].append(["alpha", "aggregate"])
        result = evalgraph.run(config, self.request)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.dropped["denied"], 0)

    def test_a_diamond_of_rebut_nodes_does_not_duplicate_findings(self):
        self.register("alpha", [self.finding(key="k")])
        rebutter = RecordingRebutter()
        evalgraph.register_rebutter("record", lambda **options: rebutter)
        nodes = [{"id": "alpha", "type": "evaluator", "evaluator": "alpha"}]
        edges = []
        previous = "alpha"
        for layer in range(2):
            left, right, join = f"left{layer}", f"right{layer}", f"join{layer}"
            nodes += [
                {"id": name, "type": "rebut", "rebutter": "record"} for name in (left, right, join)
            ]
            edges += [[previous, left], [previous, right], [left, join], [right, join]]
            previous = join
        nodes.append({"id": "aggregate", "type": "aggregate"})
        edges.append([previous, "aggregate"])
        result = evalgraph.run(
            {"nodes": nodes, "edges": edges, "allow_scores": False}, self.request
        )
        self.assertEqual(result.errors, [])
        self.assertEqual(rebutter.counts, [1] * 6)
        self.assertEqual([finding.key for finding in result.findings], ["k"])

    def test_duplicates_merge_the_viewpoints_and_the_severity(self):
        self.register(
            "alpha",
            [self.finding(viewpoint="cost", severity=Severity.MINOR, claim="first", key="same")],
        )
        self.register(
            "beta",
            [self.finding(viewpoint="budget", severity=Severity.MAJOR, claim="second", key="same")],
        )
        result = evalgraph.run(self.config(["alpha", "beta"]), self.request)
        self.assertEqual(len(result.findings), 1)
        merged = result.findings[0]
        self.assertEqual(merged.viewpoint, "cost")
        self.assertEqual(merged.viewpoints, ("cost", "budget"))
        self.assertEqual(merged.severity, Severity.MAJOR)
        self.assertEqual(merged.claim, "first")
        self.assertEqual(merged.key, "same")
        self.assertEqual(merged.evaluators, ("alpha", "beta"))
        self.assertEqual(len(merged.evidence), 1)

    def test_the_order_is_severity_then_viewpoints_then_key(self):
        self.register(
            "alpha",
            [
                self.finding(severity=Severity.CRITICAL, key="crit"),
                self.finding(severity=Severity.MAJOR, key="multi"),
                self.finding(viewpoint="safety", severity=Severity.MAJOR, key="b"),
            ],
        )
        self.register(
            "beta",
            [
                self.finding(viewpoint="budget", severity=Severity.MAJOR, key="multi"),
                self.finding(viewpoint="safety", severity=Severity.MAJOR, key="a"),
            ],
        )
        result = evalgraph.run(self.config(["alpha", "beta"]), self.request)
        self.assertEqual([finding.key for finding in result.findings], ["crit", "multi", "a", "b"])

    def test_an_evaluator_exception_is_an_error_and_the_run_continues(self):
        evaluate.register("boom", lambda **options: BoomEvaluator())
        self.register("alpha", [self.finding(key="a")])
        result = evalgraph.run(self.config(["boom", "alpha"]), self.request)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("RuntimeError: boom", result.errors[0])
        self.assertIn("'boom'", result.errors[0])
        self.assertEqual([finding.key for finding in result.findings], ["a"])

    def test_a_non_list_result_is_an_error(self):
        evaluate.register("bad", lambda **options: NonListEvaluator())
        result = evalgraph.run(self.config(["bad"]), self.request)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("not a list", result.errors[0])

    def test_a_viewpoint_outside_the_run_is_an_error(self):
        outside = Viewpoint(name="mine", description="Outside the run.")

        class Outside:
            def viewpoints(self):
                return [outside]

            def evaluate(self, request):
                return []

        evaluate.register("outside", lambda **options: Outside())
        result = evalgraph.run(self.config(["outside"]), self.request)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("not a viewpoint of this run", result.errors[0])

    def test_a_node_viewpoint_the_evaluator_lacks_is_an_error(self):
        self.register("alpha", [self.finding(key="a")], [evaluate.viewpoint("cost")])
        config = self.config(["alpha"])
        config["nodes"][0]["viewpoints"] = ["cost", "budget"]
        result = evalgraph.run(config, self.request)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("'budget'", result.errors[0])
        self.assertEqual([finding.key for finding in result.findings], ["a"])

    def test_a_rebutter_exception_passes_the_findings_through(self):
        class Boom:
            def rebut(self, findings, request):
                raise RuntimeError("nope")

        self.register("alpha", [self.finding(key="k")])
        evalgraph.register_rebutter("boom", lambda **options: Boom())
        result = evalgraph.run(self.rebut_config(rebutter="boom"), self.request)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("RuntimeError: nope", result.errors[0])
        self.assertEqual([finding.key for finding in result.findings], ["k"])

    def test_a_verdict_for_an_unknown_key_is_ignored(self):
        self.register("alpha", [self.finding(key="k")])
        self.register_rebutter("deny", [evalgraph.Verdict("other", "deny")])
        result = evalgraph.run(self.rebut_config(), self.request)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("'other'", result.errors[0])
        self.assertEqual([finding.key for finding in result.findings], ["k"])
        self.assertEqual(result.dropped["denied"], 0)

    def test_scores_are_none_unless_the_config_allows_them(self):
        recorder = RecordingEvaluator()
        evaluate.register("recorder", lambda **options: recorder)
        request = replace(self.request, scores={"contract": [{"score": 1}]})
        evalgraph.run(self.config(["recorder"]), request)
        self.assertEqual(len(recorder.requests), 1)
        self.assertIsNone(recorder.requests[0].scores)

    def test_scores_are_passed_when_the_config_allows_them(self):
        recorder = RecordingEvaluator()
        evaluate.register("recorder", lambda **options: recorder)
        request = replace(self.request, scores={"contract": [{"score": 1}]})
        evalgraph.run(self.config(["recorder"], allow_scores=True), request)
        self.assertEqual(recorder.requests[0].scores, {"contract": [{"score": 1}]})
        self.assertIs(recorder.requests[0].scores, request.scores)

    def test_judge_nodes_are_hidden_unless_scores_are_allowed(self):
        task_dir = self.root / "judge"
        tracer = trace.Tracer(FileSink(task_dir), run_id="r1")
        try:
            with trace.use(tracer):
                with trace.span(trace.Kind.TASK):
                    judge_id = trace.event(trace.JUDGE_PREFIX + "score", score=1)
        finally:
            tracer.close()
        request = EvalRequest(
            graph=graph.build([task_dir / FILE_NAME], include_judge=True),
            summaries={},
            viewpoints=evaluate.VIEWPOINTS,
        )
        recorder = RecordingEvaluator(judge_id)
        evaluate.register("recorder", lambda **options: recorder)
        config = self.config(["recorder"])
        evalgraph.run(config, request)
        self.assertIsNone(recorder.seen[0])
        config["allow_scores"] = True
        evalgraph.run(config, request)
        self.assertIsNotNone(recorder.seen[1])

    def test_an_unknown_evaluator_is_a_config_error(self):
        with self.assertRaises(ValueError):
            evalgraph.run(self.config(["missing"]), self.request)


if __name__ == "__main__":
    unittest.main()
