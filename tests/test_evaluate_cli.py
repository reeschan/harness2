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

"""Tests for `harness2.cli.evaluate`: the files it writes and the exit codes it returns."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from harness2.cli import evaluate as cli_evaluate
from harness2.core import evaluate, trace
from harness2.core.evaluate import Evidence, Finding
from harness2.core.trace_file import FileSink
from harness2.core.trace_source import Origin, SourcedEvent, SourceRead


def emit_task(*, judge=False):
    """A healthy k=1 task: one base, two candidates, one final and three stages."""

    def body():
        with trace.span(trace.Kind.TASK, task_id="task-1", k=1):
            with trace.span(trace.Kind.ROLLOUT, role="base", mode="", label="base0"):
                pass
            if judge:
                trace.event("judge.score", score=1.0, label="j0")
            with trace.span(trace.Kind.MODE, mode="agg"):
                for index in range(2):
                    with trace.span(
                        trace.Kind.ROLLOUT, role="candidate", mode="agg", label=f"c{index}"
                    ):
                        pass
                with trace.span(trace.Kind.ROLLOUT, role="final", mode="agg", label="f0"):
                    pass
                for index in range(3):
                    with trace.span(trace.Kind.STAGE, label=f"s{index}", mode="agg"):
                        pass
                trace.event(trace.Kind.COST, mode="agg", usd=0.5, rollout_usd=0.0)

    return body


class ScoreRecorder:
    """An evaluator that records the requests it sees and returns no findings."""

    def __init__(self):
        self.requests = []

    def viewpoints(self):
        return [evaluate.viewpoint("cost")]

    def evaluate(self, request):
        self.requests.append(request)
        return []


class OneViewpoint:
    """An evaluator that always reports one minor finding for a fixed viewpoint."""

    def __init__(self, viewpoint):
        self.viewpoint = viewpoint

    def viewpoints(self):
        return [evaluate.viewpoint(self.viewpoint)]

    def evaluate(self, request):
        run_id, task = sorted(request.graph.runs().items())[0]
        return [
            Finding(
                self.viewpoint,
                "minor",
                f"old {self.viewpoint}",
                (Evidence(run_id, task.span_id),),
            )
        ]


LATENCY = evaluate.Viewpoint(
    name="latency",
    description="How long the run took.",
    reads=(trace.Kind.TASK,),
)


class LatencyEvaluator:
    """An evaluator loaded as `module:Attr` that declares `latency` next to the built-in cost.

    Every instance records its `evaluate` calls in the class list `calls`.
    """

    calls = []
    declared = (LATENCY, evaluate.viewpoint("cost"))

    def viewpoints(self):
        return list(self.declared)

    def evaluate(self, request):
        type(self).calls.append(request)
        run_id, task = sorted(request.graph.runs().items())[0]
        evidence = (Evidence(run_id, task.span_id),)
        return [
            Finding("latency", "minor", "slow run", evidence, key="slow"),
            Finding("cost", "minor", "dear run", evidence, key="dear"),
        ]


class RedefiningEvaluator(LatencyEvaluator):
    """An evaluator that declares the built-in `cost` with another description."""

    calls = []
    declared = (
        evaluate.Viewpoint(
            name="cost",
            description="Something else.",
            reads=evaluate.viewpoint("cost").reads,
        ),
    )


RECORDS_NAME = "records.jsonl"


class FakeSource:
    """A trace source loaded as `module:Attr`: one JSON event per line of `records.jsonl`.

    A line that is not a JSON event is a warning naming the file and the line.
    """

    def read(self, location):
        path = Path(location) / RECORDS_NAME
        events, warnings = [], []
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
            if not line.strip():
                continue
            try:
                event = trace.TraceEvent.from_dict(json.loads(line))
            except (AttributeError, TypeError, ValueError, KeyError) as exc:
                warnings.append(f"{path}:{number}: {exc}")
                continue
            events.append(SourcedEvent(event, Origin("orchestrator", path, number)))
        return SourceRead(events, warnings)


class FileSource(FakeSource):
    """`FakeSource` whose location is the `records.jsonl` file itself."""

    def read(self, location):
        return super().read(Path(location).parent)


class BrokenSource:
    """A trace source whose `read` fails with an exception that is not an OSError."""

    def read(self, location):
        raise RuntimeError("broken reader")


class BadShapeSource:
    """A trace source whose `read` returns something that is not an (events, warnings) pair."""

    def read(self, location):
        return None


def external_records(run_id="ext-1"):
    """An external run that would break the rules if it were a harness2 run.

    The task has a `k` and no ledger, and the step failed; a built-in `error` event is
    skipped with a warning.
    """
    ts = "2026-01-01T00:00:00+00:00"

    def line(span_id, kind, parent_id=None, **attrs):
        return {
            "run_id": run_id,
            "span_id": span_id,
            "parent_id": parent_id,
            "kind": kind,
            "ts": ts,
            "attrs": attrs,
        }

    return [
        line("t", "task", phase="start", system="orchestrator", task_id="pbi-1", k=3),
        line("q", "orchestrator.question.asked", "t", topic="scope"),
        line("s", "orchestrator.step", "t", phase="start", label="build"),
        line("s", "orchestrator.step", "t", phase="end", status="error", error="boom"),
        line("e", trace.Kind.ERROR, "t", error="boom"),
        line("t", "task", phase="end", status="error"),
    ]


class EvaluateCliTests(unittest.TestCase):
    """A temporary runs directory, a clean evaluator registry and quiet output."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self._registry = dict(evaluate._REGISTRY)
        self.addCleanup(self._restore_registry)

    def _restore_registry(self):
        evaluate._REGISTRY.clear()
        evaluate._REGISTRY.update(self._registry)

    def make_run(self, run_id="run-1", *, judge=False, ledger_usd=0.0):
        task_dir = self.runs / run_id
        task_dir.mkdir(parents=True, exist_ok=True)
        tracer = trace.Tracer(FileSink(task_dir), run_id=run_id)
        try:
            with trace.use(tracer):
                emit_task(judge=judge)()
        finally:
            tracer.close()
        summary = {
            "task_id": "task-1",
            "k": 1,
            "status": "ok",
            "modes": {
                "agg": {
                    "ok": True,
                    "ledger": {
                        "R": 4,
                        "expected_R": 4,
                        "P": 3,
                        "expected_P": 3,
                        "rollout_usd": ledger_usd,
                    },
                }
            },
            "models": {"executor": [{"cost_usd": 0.5}]},
        }
        (task_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return task_dir

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_evaluate.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_writes_findings_and_the_cost_viewpoint(self):
        self.make_run()
        code, out, _ = self.run_main(["--runs", str(self.runs)])
        self.assertEqual(code, 0)
        out_dir = self.runs / "evaluation"
        findings_path = out_dir / "findings.json"
        self.assertTrue(findings_path.is_file())
        self.assertTrue((out_dir / "findings" / "cost.json").is_file())
        self.assertIn(str(findings_path), out)
        data = json.loads(findings_path.read_text())
        self.assertEqual(data["traces"], 1)
        self.assertEqual(data["runs"], 1)
        self.assertIn("dropped", data)
        self.assertIn("errors", data)
        self.assertTrue(any("cost" in item["viewpoints"] for item in data["findings"]))
        per_viewpoint = json.loads((out_dir / "findings" / "cost.json").read_text())
        self.assertTrue(any(item["viewpoint"] == "cost" for item in per_viewpoint))

    def test_out_flag_chooses_the_directory(self):
        self.make_run()
        out_dir = self.root / "elsewhere"
        code, _, _ = self.run_main(["--runs", str(self.runs), "--out", str(out_dir)])
        self.assertEqual(code, 0)
        self.assertTrue((out_dir / "findings.json").is_file())
        self.assertFalse((self.runs / "evaluation").exists())

    def test_no_traces_is_exit_2(self):
        code, _, err = self.run_main(["--runs", str(self.runs)])
        self.assertEqual(code, 2)
        self.assertIn("trace.jsonl", err)

    def test_a_broken_graph_config_is_exit_2(self):
        self.make_run()
        config = self.root / "graph.json"
        config.write_text(
            json.dumps(
                {"nodes": [{"id": "rules", "type": "evaluator", "evaluator": "rules"}], "edges": []}
            ),
            encoding="utf-8",
        )
        code, _, err = self.run_main(["--runs", str(self.runs), "--graph", str(config)])
        self.assertEqual(code, 2)
        self.assertIn("aggregate", err)

    def test_unknown_evaluator_is_exit_2(self):
        self.make_run()
        code, _, err = self.run_main(["--runs", str(self.runs), "--evaluators", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("nope", err)

    def test_allow_scores_gates_judge_nodes_and_scores(self):
        self.make_run(judge=True)
        recorders = []

        def factory(**options):
            recorder = ScoreRecorder()
            recorders.append(recorder)
            return recorder

        evaluate.register("score-recorder", factory)

        code, _, _ = self.run_main(["--runs", str(self.runs), "--evaluators", "score-recorder"])
        self.assertEqual(code, 0)
        quiet = recorders[-1].requests[-1]
        self.assertIsNone(quiet.scores)
        self.assertFalse(
            any(node.kind.startswith(trace.JUDGE_PREFIX) for node in quiet.graph.nodes.values())
        )

        code, _, _ = self.run_main(
            ["--runs", str(self.runs), "--evaluators", "score-recorder", "--allow-scores"]
        )
        self.assertEqual(code, 0)
        loud = recorders[-1].requests[-1]
        self.assertIsNotNone(loud.scores)
        self.assertEqual([item["score"] for item in loud.scores["run-1"]], [1.0])
        self.assertTrue(
            any(node.kind.startswith(trace.JUDGE_PREFIX) for node in loud.graph.nodes.values())
        )

    def test_graph_warnings_are_written_and_printed(self):
        task_dir = self.make_run()
        (task_dir / "summary.json").write_text("{", encoding="utf-8")
        code, _, err = self.run_main(["--runs", str(self.runs)])
        self.assertEqual(code, 0)
        data = json.loads((self.runs / "evaluation" / "findings.json").read_text())
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("summary.json", data["warnings"][0])
        self.assertEqual(err.count("warning: "), 1)
        self.assertIn(data["warnings"][0], err)

    def test_rerun_removes_stale_viewpoint_files(self):
        self.make_run()
        evaluate.register("onecost", lambda **options: OneViewpoint("cost"))
        evaluate.register("onesafety", lambda **options: OneViewpoint("safety"))
        code, _, _ = self.run_main(["--runs", str(self.runs), "--evaluators", "onecost"])
        self.assertEqual(code, 0)
        findings_dir = self.runs / "evaluation" / "findings"
        self.assertTrue((findings_dir / "cost.json").is_file())
        code, _, _ = self.run_main(["--runs", str(self.runs), "--evaluators", "onesafety"])
        self.assertEqual(code, 0)
        self.assertFalse((findings_dir / "cost.json").exists())
        self.assertTrue((findings_dir / "safety.json").is_file())
        data = json.loads((self.runs / "evaluation" / "findings.json").read_text())
        self.assertEqual([item["viewpoint"] for item in data["findings"]], ["safety"])

    def write_graph(self, evaluator, viewpoints):
        """A graph config of one `module:Attr` evaluator node choosing `viewpoints`."""
        path = self.root / "graph.json"
        path.write_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "id": "fake",
                            "type": "evaluator",
                            "evaluator": f"{__name__}:{evaluator}",
                            "viewpoints": viewpoints,
                        },
                        {"id": "aggregate", "type": "aggregate"},
                    ],
                    "edges": [["fake", "aggregate"]],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_a_declared_viewpoint_gets_its_own_findings_file(self):
        self.make_run()
        LatencyEvaluator.calls.clear()
        graph = self.write_graph("LatencyEvaluator", ["latency"])
        code, _, err = self.run_main(["--runs", str(self.runs), "--graph", str(graph)])
        self.assertEqual(code, 0, err)
        out_dir = self.runs / "evaluation"
        data = json.loads((out_dir / "findings.json").read_text())
        self.assertEqual(data["errors"], [])
        self.assertEqual([item["key"] for item in data["findings"]], ["slow"])
        self.assertEqual(data["findings"][0]["viewpoints"], ["latency"])
        latency = json.loads((out_dir / "findings" / "latency.json").read_text())
        self.assertEqual([item["claim"] for item in latency], ["slow run"])
        self.assertEqual(
            sorted(path.name for path in (out_dir / "findings").iterdir()), ["latency.json"]
        )
        self.assertEqual(LatencyEvaluator.calls[0].viewpoints, (LATENCY,))

    def test_a_viewpoint_no_evaluator_declares_is_exit_2(self):
        self.make_run()
        LatencyEvaluator.calls.clear()
        graph = self.write_graph("LatencyEvaluator", ["throughput"])
        code, _, err = self.run_main(["--runs", str(self.runs), "--graph", str(graph)])
        self.assertEqual(code, 2)
        self.assertIn("throughput", err)
        self.assertEqual(LatencyEvaluator.calls, [])
        self.assertFalse((self.runs / "evaluation").exists())

    def test_a_redefined_built_in_viewpoint_is_exit_2(self):
        self.make_run()
        RedefiningEvaluator.calls.clear()
        graph = self.write_graph("RedefiningEvaluator", ["cost"])
        code, _, err = self.run_main(["--runs", str(self.runs), "--graph", str(graph)])
        self.assertEqual(code, 2)
        self.assertIn("'cost'", err)
        self.assertEqual(RedefiningEvaluator.calls, [])
        self.assertFalse((self.runs / "evaluation").exists())

    def test_a_bad_viewpoint_name_in_the_graph_is_exit_2(self):
        self.make_run()
        graph = self.write_graph("LatencyEvaluator", ["Latency"])
        code, _, err = self.run_main(["--runs", str(self.runs), "--graph", str(graph)])
        self.assertEqual(code, 2)
        self.assertIn("[a-z][a-z0-9_]*", err)

    def test_graph_config_gates_scores_with_the_flag(self):
        self.make_run(judge=True)
        recorders = []

        def factory(**options):
            recorder = ScoreRecorder()
            recorders.append(recorder)
            return recorder

        evaluate.register("score-recorder", factory)
        for config_allow in (True, False):
            config = self.root / f"graph-{config_allow}.json"
            config.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {"id": "rec", "type": "evaluator", "evaluator": "score-recorder"},
                            {"id": "agg", "type": "aggregate"},
                        ],
                        "edges": [["rec", "agg"]],
                        "allow_scores": config_allow,
                    }
                ),
                encoding="utf-8",
            )
            for flag in (False, True):
                with self.subTest(config_allow=config_allow, flag=flag):
                    code, _, _ = self.run_main(
                        ["--runs", str(self.runs), "--graph", str(config)]
                        + (["--allow-scores"] if flag else [])
                    )
                    self.assertEqual(code, 0)
                    request = recorders[-1].requests[-1]
                    judge_nodes = [
                        node
                        for node in request.graph.nodes.values()
                        if node.kind.startswith(trace.JUDGE_PREFIX)
                    ]
                    if config_allow and flag:
                        self.assertEqual([item["score"] for item in request.scores["run-1"]], [1.0])
                        self.assertTrue(judge_nodes)
                    else:
                        self.assertIsNone(request.scores)
                        self.assertFalse(judge_nodes)

    def make_records(self, records=None, folder="records"):
        """A directory with `records.jsonl` for `FakeSource`."""
        location = self.root / folder
        location.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(item) for item in (external_records() if records is None else records)]
        (location / RECORDS_NAME).write_text("".join(f"{line}\n" for line in lines), "utf-8")
        return location

    def register_recorder(self):
        recorders = []

        def factory(**options):
            recorder = ScoreRecorder()
            recorders.append(recorder)
            return recorder

        evaluate.register("score-recorder", factory)
        return recorders

    def test_a_source_alone_is_evaluated_and_rules_leaves_the_external_run_alone(self):
        location = self.make_records()
        with (location / RECORDS_NAME).open("a", encoding="utf-8") as handle:
            handle.write("not json\n")
        code, out, err = self.run_main(["--source", f"{__name__}:FakeSource={location}"])
        self.assertEqual(code, 0, err)
        findings_path = location / "evaluation" / "findings.json"
        self.assertIn(str(findings_path), out)
        data = json.loads(findings_path.read_text())
        self.assertEqual(data["traces"], 0)
        self.assertEqual(data["runs"], 1)
        self.assertEqual(data["sources"], {"orchestrator": 1})
        self.assertEqual(data["findings"], [])
        self.assertEqual(data["errors"], [])
        self.assertEqual(len(data["warnings"]), 2)
        self.assertTrue(any("built-in kind" in item for item in data["warnings"]))
        self.assertTrue(any(f"{RECORDS_NAME}:7" in item for item in data["warnings"]))
        self.assertEqual(err.count("warning: "), 2)

    def test_runs_and_a_source_make_one_run_graph(self):
        task_dir = self.make_run(ledger_usd=1.0)
        location = self.make_records()
        source = f"{__name__}:FakeSource={location}"
        recorders = self.register_recorder()
        code, _, err = self.run_main(
            ["--runs", str(self.runs), "--source", source, "--evaluators", "score-recorder"]
        )
        self.assertEqual(code, 0, err)
        request = recorders[-1].requests[-1]
        self.assertEqual(set(request.graph.runs()), {"run-1", "ext-1"})
        self.assertEqual(set(request.summaries), {task_dir})
        self.assertIsNotNone(request.summary_for("run-1"))
        self.assertIsNone(request.summary_for("ext-1"))
        task = request.graph.runs()["ext-1"]
        self.assertEqual(task.source, Origin("orchestrator", location / RECORDS_NAME, 1))
        self.assertEqual(request.graph.find("ext-1", "s").source.line, 3)
        self.assertEqual(request.graph.runs()["run-1"].source, task_dir / "trace.jsonl")

        code, _, err = self.run_main(["--runs", str(self.runs), "--source", source])
        self.assertEqual(code, 0, err)
        data = json.loads((self.runs / "evaluation" / "findings.json").read_text())
        self.assertEqual(data["traces"], 1)
        self.assertEqual(data["runs"], 2)
        self.assertEqual(data["sources"], {"orchestrator": 1})
        self.assertTrue(data["findings"])
        evidence_runs = {
            item["run_id"] for finding in data["findings"] for item in finding["evidence"]
        }
        self.assertEqual(evidence_runs, {"run-1"})
        self.assertEqual(data["errors"], [])

    def test_runs_means_the_same_as_the_trace_source(self):
        task_dir = self.make_run(ledger_usd=1.0)
        (task_dir / "broken").mkdir()
        (task_dir / "broken" / "trace.jsonl").write_text("{\n", encoding="utf-8")
        recorders = self.register_recorder()
        outputs = []
        for argv in (["--runs", str(self.runs)], ["--source", f"trace={self.runs}"]):
            for evaluators in ("rules", "score-recorder"):
                with self.subTest(argv=argv, evaluators=evaluators):
                    code, _, err = self.run_main(argv + ["--evaluators", evaluators])
                    self.assertEqual(code, 0, err)
                    if evaluators == "rules":
                        outputs.append((self.runs / "evaluation" / "findings.json").read_text())
        self.assertEqual(outputs[0], outputs[1])
        data = json.loads(outputs[0])
        self.assertEqual((data["traces"], data["runs"], data["sources"]), (2, 1, {}))
        self.assertTrue(data["findings"])
        by_runs, by_source = (recorder.requests[-1] for recorder in recorders)
        self.assertEqual(by_runs.graph.nodes, by_source.graph.nodes)
        self.assertEqual(by_runs.graph.edges, by_source.graph.edges)
        self.assertEqual(by_runs.summaries, by_source.summaries)

    def test_an_unknown_trace_source_is_exit_2(self):
        self.make_run()
        code, _, err = self.run_main(["--runs", str(self.runs), "--source", f"nope={self.runs}"])
        self.assertEqual(code, 2)
        self.assertIn("nope", err)
        self.assertFalse((self.runs / "evaluation").exists())

    def test_no_trace_and_no_trace_source_run_is_exit_2(self):
        location = self.make_records([])
        code, _, err = self.run_main(["--source", f"{__name__}:FakeSource={location}"])
        self.assertEqual(code, 2)
        self.assertIn("trace source run", err)
        self.assertFalse((location / "evaluation").exists())

    def test_a_file_location_writes_next_to_the_file(self):
        location = self.make_records()
        records = location / RECORDS_NAME
        code, out, err = self.run_main(["--source", f"{__name__}:FileSource={records}"])
        self.assertEqual(code, 0, err)
        findings_path = location / "evaluation" / "findings.json"
        self.assertIn(str(findings_path), out)
        data = json.loads(findings_path.read_text())
        self.assertEqual(data["sources"], {"orchestrator": 1})

    def test_a_failing_trace_source_is_a_warning_and_the_run_goes_on(self):
        self.make_run()
        location = self.make_records()
        for name in ("BrokenSource", "BadShapeSource"):
            with self.subTest(name=name):
                source = f"{__name__}:{name}"
                code, _, err = self.run_main(
                    ["--runs", str(self.runs), "--source", f"{source}={location}"]
                )
                self.assertEqual(code, 0, err)
                data = json.loads((self.runs / "evaluation" / "findings.json").read_text())
                self.assertEqual((data["traces"], data["runs"], data["sources"]), (1, 1, {}))
                expected = f"trace source {source} failed on {location}: "
                self.assertTrue(any(item.startswith(expected) for item in data["warnings"]))
                self.assertIn(f"warning: {expected}", err)

    def test_neither_runs_nor_source_and_a_bad_source_are_exit_2(self):
        for argv in ([], ["--source", "no-location"], ["--source", "=somewhere"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_main(argv)
                self.assertEqual(code, 2)
                self.assertIn("error:", err)


if __name__ == "__main__":
    unittest.main()
