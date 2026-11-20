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

"""Tests for `harness2.core.graph`."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from harness2.core import graph, trace
from harness2.core.evaluate import VIEWPOINTS, EvalRequest
from harness2.core.trace_file import FILE_NAME, FileSink
from harness2.core.trace_source import Origin, SourcedEvent, SourceRead

RECORDS = Path("records") / "log.jsonl"


def sourced(
    kind, span_id, line, *, run_id="x1", parent_id=None, ts="2026-01-01T00:00:00+00:00", **attrs
):
    """One event an `orchestrator` trace source read from `line` of `RECORDS`."""
    return SourcedEvent(
        trace.TraceEvent(
            run_id=run_id,
            span_id=span_id,
            parent_id=parent_id,
            kind=kind,
            ts=ts,
            attrs=attrs,
        ),
        Origin("orchestrator", RECORDS, line),
    )


def external_run(run_id="x1"):
    """The events of one well-formed external run: a task span and two events inside it."""
    return [
        sourced(trace.Kind.TASK, "t", 1, run_id=run_id, phase="start", system="orchestrator"),
        sourced("orchestrator.question.asked", "q", 2, run_id=run_id, parent_id="t"),
        sourced("orchestrator.step", "s", 3, run_id=run_id, parent_id="t", phase="start"),
        sourced(
            "orchestrator.step", "s", 4, run_id=run_id, parent_id="t", phase="end", status="ok"
        ),
        sourced(trace.Kind.TASK, "t", 5, run_id=run_id, phase="end", status="ok"),
    ]


def make_tree(count):
    """A graph of `count` nodes: one `task` root and the rest in a 3-way tree below it."""
    built = graph.Graph()
    for index in range(count):
        span_id = f"s{index:05d}"
        key = ("r", span_id)
        if index == 0:
            parent = None
            kind = trace.Kind.TASK
        else:
            parent = ("r", f"s{(index - 1) // 3:05d}")
            kind = trace.Kind.ROLLOUT if index % 4 == 0 else trace.Kind.STAGE
        built.nodes[key] = graph.Node(
            key=key,
            kind=kind,
            run_id="r",
            span_id=span_id,
            parent=parent,
            attrs={},
            start_ts=None,
            end_ts=None,
            status=None,
            dur_s=None,
            source=Path("trace.jsonl"),
        )
        if parent is not None:
            built.edges.append(graph.Edge(parent, key))
    return built


def walk_ancestors(built, key):
    """The ancestors of `key` walked over `edges`, nearest first."""
    chain = []
    seen = {key}
    while True:
        source = next((edge.src for edge in built.edges if edge.dst == key), None)
        if source is None or source in seen:
            return chain
        chain.append(source)
        seen.add(source)
        key = source


class GraphCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make_tracer(self, folder="run", *, run_id=None):
        task_dir = self.root / folder
        tracer = trace.Tracer(FileSink(task_dir), run_id=run_id)
        self.addCleanup(tracer.close)
        return task_dir, tracer

    def trace_path(self, folder="run"):
        return self.root / folder / FILE_NAME

    def build(self, folder="run", **kwargs):
        return graph.build([self.trace_path(folder)], **kwargs)


class BuildTests(GraphCase):
    def test_start_and_end_merge_into_one_node(self):
        _, tracer = self.make_tracer(run_id="r1")
        with trace.use(tracer):
            with trace.span(trace.Kind.STAGE, label="start", dim="INV") as span:
                span.set(label="end", attempts=1)
                span_id = span.id
        tracer.close()

        built = self.build()
        self.assertEqual(len(built.nodes), 1)
        node = built.find("r1", span_id)
        self.assertIsNotNone(node)
        self.assertEqual(node.kind, trace.Kind.STAGE)
        self.assertEqual(node.attrs["label"], "end")
        self.assertEqual(node.attrs["dim"], "INV")
        self.assertEqual(node.attrs["attempts"], 1)
        self.assertNotIn("phase", node.attrs)
        self.assertEqual(node.status, "ok")
        self.assertEqual(node.attrs["status"], "ok")
        self.assertIsInstance(node.dur_s, float)
        self.assertEqual(node.attrs["dur_s"], node.dur_s)
        self.assertIsNotNone(node.start_ts)
        self.assertIsNotNone(node.end_ts)
        self.assertEqual(node.source, self.trace_path())

    def test_failed_span_takes_its_status_from_the_end(self):
        _, tracer = self.make_tracer(run_id="r1")
        with self.assertRaises(RuntimeError):
            with trace.use(tracer):
                with trace.span(trace.Kind.STAGE) as span:
                    span_id = span.id
                    raise RuntimeError("boom")
        tracer.close()

        node = self.build().find("r1", span_id)
        self.assertEqual(node.status, "error")
        self.assertEqual(node.attrs["error"], "RuntimeError: boom")

    def test_point_event_is_a_node_without_a_status(self):
        _, tracer = self.make_tracer(run_id="r1")
        with trace.use(tracer):
            span_id = trace.event(trace.Kind.COST, usd=0.25)
        tracer.close()

        node = self.build().find("r1", span_id)
        self.assertIsNotNone(node)
        self.assertEqual(node.kind, trace.Kind.COST)
        self.assertEqual(node.attrs["usd"], 0.25)
        self.assertIsNone(node.status)
        self.assertIsNone(node.dur_s)
        self.assertIsNone(node.end_ts)
        self.assertIsNotNone(node.start_ts)

    def test_edges_follow_the_parent_links(self):
        _, tracer = self.make_tracer(run_id="r1")
        with trace.use(tracer):
            with trace.span(trace.Kind.TASK, task_id="t1") as task:
                with trace.span(trace.Kind.STAGE, label="s") as stage:
                    diff_id = trace.event(trace.Kind.TREE_DIFF, files=[])
                cost_id = trace.event(trace.Kind.COST, usd=0.1)
            task_id, stage_id = task.id, stage.id
        tracer.close()

        built = self.build()
        self.assertEqual([node.key for node in built.roots()], [("r1", task_id)])
        self.assertEqual(built.parent(("r1", diff_id)).key, ("r1", stage_id))
        self.assertEqual(
            {node.span_id for node in built.children(("r1", task_id))}, {stage_id, cost_id}
        )
        self.assertEqual([node.span_id for node in built.children(("r1", stage_id))], [diff_id])
        self.assertIsNone(built.parent(("r1", task_id)))
        self.assertEqual(
            [node.span_id for node in built.ancestors(("r1", diff_id))], [stage_id, task_id]
        )
        self.assertEqual([node.span_id for node in built.of_kind(trace.Kind.COST)], [cost_id])
        self.assertTrue(all(edge.kind == graph.CONTAINS for edge in built.edges))

    def test_two_runs_in_one_file_are_separate_trees(self):
        task_dir = self.root / "run"
        task_ids = {}
        for run_id in ("r1", "r2"):
            tracer = trace.Tracer(FileSink(task_dir), run_id=run_id)
            with trace.use(tracer):
                with trace.span(trace.Kind.TASK, task_id=run_id) as task:
                    task_ids[run_id] = task.id
                    trace.event(trace.Kind.COST, usd=0.1)
            tracer.close()

        built = self.build()
        self.assertEqual(len(built.roots()), 2)
        self.assertEqual(set(built.runs()), {"r1", "r2"})
        self.assertEqual(built.runs()["r1"].span_id, task_ids["r1"])
        self.assertEqual(built.runs()["r2"].span_id, task_ids["r2"])
        self.assertEqual({node.run_id for node in built.nodes.values()}, {"r1", "r2"})
        self.assertTrue(all(edge.src[0] == edge.dst[0] for edge in built.edges))

    def test_subgraph_relinks_to_the_nearest_kept_ancestor(self):
        _, tracer = self.make_tracer(run_id="r1")
        with trace.use(tracer):
            with trace.span(trace.Kind.TASK) as task:
                with trace.span(trace.Kind.STAGE):
                    with trace.span(trace.Kind.ROLLOUT, role="candidate") as rollout:
                        trace.event(trace.Kind.COST)
            task_id, rollout_id = task.id, rollout.id
        tracer.close()

        built = self.build()
        sub = built.subgraph(kinds={trace.Kind.TASK, trace.Kind.ROLLOUT})
        self.assertEqual(set(sub.nodes), {("r1", task_id), ("r1", rollout_id)})
        self.assertEqual(sub.parent(("r1", rollout_id)).key, ("r1", task_id))
        self.assertEqual([node.key for node in sub.children(("r1", task_id))], [("r1", rollout_id)])
        self.assertIsNone(sub.parent(("r1", task_id)))
        self.assertEqual(built.parent(("r1", rollout_id)).kind, trace.Kind.STAGE)

    def test_subgraph_filters_by_run(self):
        task_dir = self.root / "run"
        for run_id in ("r1", "r2"):
            tracer = trace.Tracer(FileSink(task_dir), run_id=run_id)
            with trace.use(tracer):
                with trace.span(trace.Kind.TASK):
                    trace.event(trace.Kind.COST, usd=0.1)
            tracer.close()

        sub = self.build().subgraph(run_ids={"r2"})
        self.assertEqual({node.run_id for node in sub.nodes.values()}, {"r2"})
        self.assertEqual(set(sub.runs()), {"r2"})
        self.assertEqual(len(sub.roots()), 1)
        self.assertTrue(all(edge.src[0] == "r2" for edge in sub.edges))

    def test_a_missing_parent_leaves_a_root_and_a_warning(self):
        task_dir, tracer = self.make_tracer(run_id="r1")
        with trace.use(tracer):
            with trace.span(trace.Kind.TASK) as task:
                task_id = task.id
        tracer.close()
        FileSink(task_dir).emit(
            trace.TraceEvent(
                run_id="r1",
                span_id="orphan",
                parent_id="missing",
                kind=trace.Kind.COST,
                ts="2026-01-01T00:00:00+00:00",
                attrs={"usd": 0.1},
            )
        )

        built = self.build()
        node = built.find("r1", "orphan")
        self.assertIsNotNone(node)
        self.assertIsNone(node.parent)
        self.assertIsNone(built.parent(("r1", "orphan")))
        self.assertEqual([root.span_id for root in built.roots()], [task_id])
        self.assertEqual(len(built.warnings), 1)
        self.assertIn("missing", built.warnings[0])

    def test_judge_kinds_are_optional(self):
        _, tracer = self.make_tracer(run_id="r1")
        with trace.use(tracer):
            with trace.span(trace.Kind.TASK):
                judge_id = trace.event(trace.JUDGE_PREFIX + "score", score=1)
        tracer.close()

        self.assertIsNone(self.build().find("r1", judge_id))
        node = self.build(include_judge=True).find("r1", judge_id)
        self.assertIsNotNone(node)
        self.assertEqual(node.kind, trace.JUDGE_PREFIX + "score")
        self.assertEqual(node.attrs["score"], 1)

    def test_find_returns_none_for_an_unknown_span(self):
        self.assertIsNone(self.build().find("nope", "nope"))

    def test_start_read_after_end_keeps_the_end_attrs(self):
        task_dir = self.root / "run"
        start_ts = "2026-01-01T00:00:00+00:00"
        end_ts = "2026-01-01T00:00:01+00:00"
        sink = FileSink(task_dir)
        sink.emit(
            trace.TraceEvent(
                run_id="r1",
                span_id="s1",
                parent_id=None,
                kind=trace.Kind.STAGE,
                ts=end_ts,
                attrs={"phase": "end", "status": "ok", "ok": False, "dur_s": 1.0},
            )
        )
        sink.emit(
            trace.TraceEvent(
                run_id="r1",
                span_id="s1",
                parent_id=None,
                kind=trace.Kind.STAGE,
                ts=start_ts,
                attrs={"phase": "start", "ok": None, "label": "s"},
            )
        )

        node = self.build().find("r1", "s1")
        self.assertIsNotNone(node)
        self.assertIs(node.attrs["ok"], False)
        self.assertEqual(node.attrs["label"], "s")
        self.assertEqual(node.attrs["status"], "ok")
        self.assertEqual(node.status, "ok")
        self.assertEqual(node.dur_s, 1.0)
        self.assertEqual(node.start_ts, start_ts)
        self.assertEqual(node.end_ts, end_ts)


class LoadRunsTests(GraphCase):
    def test_load_runs_reads_nested_traces_and_summaries(self):
        first = self.root / "a"
        second = self.root / "b" / "c"
        for task_dir in (first, second):
            tracer = trace.Tracer(FileSink(task_dir), run_id="r1")
            with trace.use(tracer):
                with trace.span(trace.Kind.TASK):
                    trace.event(trace.Kind.COST, usd=0.1)
            tracer.close()
        (first / "summary.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")

        built, summaries = graph.load_runs(self.root)
        self.assertEqual(len(built.roots()), 2)
        self.assertEqual(set(summaries), {first})
        self.assertEqual(summaries[first], {"status": "ok"})
        self.assertEqual(
            {node.source for node in built.nodes.values()},
            {first / FILE_NAME, second / FILE_NAME},
        )

    def test_load_runs_of_an_empty_root(self):
        built, summaries = graph.load_runs(self.root)
        self.assertEqual(built.nodes, {})
        self.assertEqual(summaries, {})

    def test_an_unreadable_trace_is_a_warning_and_the_rest_is_read(self):
        good = self.root / "good"
        bad = self.root / "bad"
        for task_dir in (good, bad):
            tracer = trace.Tracer(FileSink(task_dir), run_id="r1")
            with trace.use(tracer):
                with trace.span(trace.Kind.TASK):
                    trace.event(trace.Kind.COST, usd=0.1)
            tracer.close()
        if os.geteuid() == 0:
            self.skipTest("file permissions do not stop root")
        bad_trace = bad / FILE_NAME
        bad_trace.chmod(0o000)
        self.addCleanup(bad_trace.chmod, 0o644)

        built, summaries = graph.load_runs(self.root)
        self.assertEqual(len(built.roots()), 1)
        self.assertEqual({node.source for node in built.nodes.values()}, {good / FILE_NAME})
        self.assertEqual(summaries, {})
        self.assertTrue(
            any(warning.startswith(f"cannot read {bad_trace}") for warning in built.warnings)
        )

    def test_a_summary_that_is_not_a_json_object_is_a_warning(self):
        first = self.root / "a"
        second = self.root / "b"
        for task_dir in (first, second):
            tracer = trace.Tracer(FileSink(task_dir), run_id="r1")
            with trace.use(tracer):
                with trace.span(trace.Kind.TASK):
                    trace.event(trace.Kind.COST, usd=0.1)
            tracer.close()
        (first / "summary.json").write_text("[1, 2]", encoding="utf-8")
        (second / "summary.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")

        built, summaries = graph.load_runs(self.root)
        self.assertEqual(set(summaries), {second})
        self.assertEqual(len(built.roots()), 2)
        self.assertTrue(
            any(
                f"summary {first / 'summary.json'} is not a JSON object" in warning
                for warning in built.warnings
            )
        )


class SourceTests(GraphCase):
    def write_harness2_run(self, folder="run", run_id="r1"):
        task_dir, tracer = self.make_tracer(folder, run_id=run_id)
        with trace.use(tracer):
            with trace.span(trace.Kind.TASK, task_id=run_id) as task:
                trace.event(trace.Kind.COST, usd=0.1)
        tracer.close()
        return task_dir, task.id

    def test_external_nodes_carry_the_trace_source_origin(self):
        _, task_id = self.write_harness2_run()
        built = graph.build([self.trace_path()], sources=[SourceRead(external_run(), [])])
        self.assertEqual(built.warnings, [])
        self.assertEqual(set(built.runs()), {"r1", "x1"})
        self.assertEqual(built.find("r1", task_id).source, self.trace_path())
        task = built.find("x1", "t")
        self.assertEqual(task.source, Origin("orchestrator", RECORDS, 1))
        self.assertEqual(task.source.name, "orchestrator")
        self.assertEqual(task.source.file, RECORDS)
        self.assertEqual(task.source.line, 1)
        self.assertEqual(task.status, "ok")
        self.assertEqual(task.attrs["system"], "orchestrator")
        step = built.find("x1", "s")
        self.assertEqual(step.source, Origin("orchestrator", RECORDS, 3))
        self.assertEqual(step.status, "ok")
        self.assertEqual(built.parent(("x1", "s")).key, ("x1", "t"))
        self.assertEqual({node.span_id for node in built.children(("x1", "t"))}, {"q", "s"})

    def test_a_span_from_a_second_input_is_dropped_with_a_warning(self):
        events = external_run()
        again = [
            sourced(trace.Kind.TASK, "t2", 6, system="orchestrator"),
            sourced("orchestrator.step", "s", 7, parent_id="t2", label="later"),
            sourced("orchestrator.step", "s", 8, parent_id="t2", phase="end", status="error"),
        ]
        built = graph.build(sources=[(events, []), (again, [])])
        self.assertEqual(len(built.warnings), 1)
        self.assertIn("x1/s", built.warnings[0])
        self.assertIn("duplicate", built.warnings[0])
        self.assertIn("log.jsonl:7", built.warnings[0])
        self.assertIsNotNone(built.find("x1", "t2"))
        step = built.find("x1", "s")
        self.assertEqual(step.status, "ok")
        self.assertEqual(step.source.line, 3)
        self.assertNotIn("label", step.attrs)

    def test_a_trace_span_wins_over_the_same_span_from_a_trace_source(self):
        _, task_id = self.write_harness2_run()
        copy = [
            sourced(trace.Kind.TASK, "xt", 1, run_id="r1", system="orchestrator"),
            sourced("orchestrator.step", task_id, 2, run_id="r1", parent_id="xt"),
        ]
        built = graph.build([self.trace_path()], sources=[(copy, [])])
        self.assertEqual(built.find("r1", task_id).source, self.trace_path())
        self.assertEqual(built.find("r1", task_id).kind, trace.Kind.TASK)
        self.assertEqual(len(built.warnings), 1)
        self.assertIn("duplicate", built.warnings[0])

    def test_broken_events_are_warnings_and_the_rest_is_read(self):
        events = external_run() + [
            sourced(trace.Kind.TASK, "t2", 6, run_id="x2"),
            sourced(trace.Kind.MODE, "m", 7, parent_id="t"),
            sourced("step", "n", 8, parent_id="t"),
            sourced("orchestrator.orphan", "o", 9, parent_id="gone"),
        ]
        built = graph.build(sources=[SourceRead(events, ["records/log.jsonl:10: not JSON"])])
        self.assertEqual(set(built.runs()), {"x1"})
        self.assertIsNone(built.find("x1", "m"))
        self.assertIsNone(built.find("x1", "n"))
        self.assertIsNone(built.find("x2", "t2"))
        orphan = built.find("x1", "o")
        self.assertIsNotNone(orphan)
        self.assertIsNone(orphan.parent)
        self.assertEqual(len(built.warnings), 5)
        self.assertEqual(built.warnings[0], "records/log.jsonl:10: not JSON")
        self.assertIn("missing parent x1/gone", built.warnings[-1])
        self.assertIn("log.jsonl:9 (orchestrator)", built.warnings[-1])

    def test_load_runs_takes_sources_and_leaves_external_runs_out_of_the_summaries(self):
        task_dir, _ = self.write_harness2_run("a")
        (task_dir / "summary.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
        built, summaries = graph.load_runs(self.root, sources=[SourceRead(external_run(), [])])
        self.assertEqual(set(built.runs()), {"r1", "x1"})
        self.assertEqual(set(summaries), {task_dir})
        request = EvalRequest(graph=built, summaries=summaries, viewpoints=VIEWPOINTS)
        self.assertEqual(request.summary_for("r1"), {"status": "ok"})
        self.assertIsNone(request.summary_for("x1"))

    def test_load_runs_of_several_roots_or_none(self):
        first, _ = self.write_harness2_run("a", run_id="r1")
        second, _ = self.write_harness2_run("b", run_id="r2")
        built, _ = graph.load_runs([first, second])
        self.assertEqual(set(built.runs()), {"r1", "r2"})
        same, _ = graph.load_runs([self.root, first])
        self.assertEqual(set(same.nodes), set(built.nodes))
        self.assertEqual(same.warnings, [])
        empty, summaries = graph.load_runs(sources=[SourceRead(external_run(), [])])
        self.assertEqual(set(empty.runs()), {"x1"})
        self.assertEqual(summaries, {})


class IndexTests(unittest.TestCase):
    def test_indexes_agree_with_the_edges(self):
        built = make_tree(120)
        for key in built.nodes:
            expected_parent = next((edge.src for edge in built.edges if edge.dst == key), None)
            parent = built.parent(key)
            self.assertEqual(None if parent is None else parent.key, expected_parent)
            expected_children = sorted(
                {edge.dst for edge in built.edges if edge.src == key and edge.dst in built.nodes}
            )
            self.assertEqual([node.key for node in built.children(key)], expected_children)
            self.assertEqual(
                [node.key for node in built.ancestors(key)], walk_ancestors(built, key)
            )

    def test_subgraph_and_parent_stay_fast_on_a_large_graph(self):
        built = make_tree(4000)
        start = time.monotonic()
        for key in built.nodes:
            built.parent(key)
        sub = built.subgraph(kinds={trace.Kind.TASK, trace.Kind.ROLLOUT})
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 5.0)
        self.assertEqual(len(sub.nodes), 1 + sum(1 for index in range(1, 4000) if index % 4 == 0))
        self.assertEqual(sub.parent(("r", "s00040")).key, ("r", "s00004"))
        self.assertEqual(sub.parent(("r", "s00004")).key, ("r", "s00000"))

    def test_subgraph_indexes_survive_the_rebuild(self):
        built = make_tree(100)
        sub = built.subgraph(kinds={trace.Kind.TASK, trace.Kind.ROLLOUT})
        self.assertEqual(
            [node.key for node in sub.ancestors(("r", "s00040"))],
            [("r", "s00004"), ("r", "s00000")],
        )
        self.assertEqual(
            [node.key for node in sub.children(("r", "s00004"))],
            sorted(node.key for node in sub.nodes.values() if node.parent == ("r", "s00004")),
        )


if __name__ == "__main__":
    unittest.main()
