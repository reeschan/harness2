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
from harness2.core.trace_file import FileSink


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


if __name__ == "__main__":
    unittest.main()
