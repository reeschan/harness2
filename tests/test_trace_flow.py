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

"""The trace of one task run: parent links, tree diffs, summary and failures."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2.core import memory, recursion, schedule, selection, trace
from harness2.core.ledger import Ledger
from harness2.core.proposer import ProposerError, Reply
from harness2.core.spec import RunView
from harness2.demo import DemoAdapter, DemoProposer
from harness2.testing.trace_contract import RecordingSink

TASK_ID = "ledger-1"

EXPERIENCE_TEXT = (
    "<experience>\ncue: a ledger holds credits and debits\n"
    "lesson: sum both signs in scripts/summarize.py\n"
    "evidence: the ledger total changed when the filter was dropped\n"
    "confidence: high for this observed contrast\n</experience>"
)


class CostProposer:
    """A proposer that bills the selector and the reflection separately."""

    def __init__(self, *, select_usd=0.4, reflect_usd=0.1, reflect_fails=False):
        self.select_usd = select_usd
        self.reflect_usd = reflect_usd
        self.reflect_fails = reflect_fails
        self.spent = 0.0

    def call(self, mission, context_dir, label, **kwargs):
        cost = self.select_usd if label.startswith("select.") else self.reflect_usd
        self.spent += cost
        if label.startswith("select."):
            return Reply(label=label, text="```choice\nA\n```", ok=True, cost_usd=cost)
        if self.reflect_fails:
            raise ProposerError(f"[{label}] no experience", cost_usd=cost)
        return Reply(label=label, text=EXPERIENCE_TEXT, ok=True, cost_usd=cost)


class BatchAdapter:
    """An adapter whose batched executions return made-up run dirs."""

    def __init__(self, dirs, *, fail_label=""):
        self.dirs = dirs
        self.fail_label = fail_label
        self.cost_dirs = []

    def execute_many(self, task_id, harness_dir, run_ids):
        return list(self.dirs)

    def delivered(self, run_dir):
        if self.fail_label and Path(run_dir).name == self.fail_label:
            raise RuntimeError("delivered failed")
        return True

    def rollout_cost_usd(self, run_id, run_dir):
        self.cost_dirs.append(Path(run_dir))
        return 1.0 if Path(run_dir).exists() else 0.0


class DirectoryCostAdapter:
    """A one-at-a-time adapter that prices a rollout by whether its directory exists."""

    def __init__(self, returned, *, delivered_raises=False):
        self.returned = Path(returned)
        self.delivered_raises = delivered_raises
        self.cost_dirs = []

    def execute(self, task_id, harness_dir, run_id):
        return self.returned

    def delivered(self, run_dir):
        if self.delivered_raises:
            raise RuntimeError("delivered failed")
        return True

    def rollout_cost_usd(self, run_id, run_dir):
        self.cost_dirs.append(Path(run_dir))
        return 1.0 if Path(run_dir).exists() else 0.0


class EmittingDirectoryCostAdapter(DirectoryCostAdapter):
    """Reports a model route while the rollout is still running."""

    def execute(self, task_id, harness_dir, run_id):
        trace.event(trace.Kind.MODEL_ROUTE, role="executor")
        time.sleep(0.02)
        return self.returned


class CostDemoAdapter(DemoAdapter):
    """DemoAdapter with a non-zero rollout price, so a kept Ledger shows in COST."""

    def rollout_cost_usd(self, run_id, run_dir):
        return 0.5


class ExplodingSelector:
    """Delegates every call but fails the selector with an unexpected error."""

    def __init__(self, inner):
        self.inner = inner

    def call(self, mission, context_dir, label, **kwargs):
        if label.startswith("select."):
            raise RuntimeError("selector exploded")
        return self.inner.call(mission, context_dir, label, **kwargs)


class SingleAdapter:
    """An adapter with no batched path, so every rollout runs on its own."""

    def __init__(self, dirs):
        self.dirs = dirs

    def execute(self, task_id, harness_dir, run_id):
        return self.dirs[0]

    def delivered(self, run_dir):
        return True

    def rollout_cost_usd(self, run_id, run_dir):
        return 1.0


class BrokenSink:
    """A sink that fails on every emit, to prove the run goes on."""

    def emit(self, event):
        raise RuntimeError("broken sink")

    def close(self):
        pass


def broken_sink(root, **options):
    """The factory form `factory(root=Path, **options)`."""
    return BrokenSink()


class TraceFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adapter = DemoAdapter(self.root)
        self.proposer = DemoProposer(self.adapter.spec.channels["proposer"])

    def run_one(self, k=1):
        return schedule.run_task(
            TASK_ID,
            k,
            adapter=self.adapter,
            prop=self.proposer,
            runs_root=self.adapter.spec.runs_root,
        )

    def task_dir(self, k=1):
        return self.adapter.spec.runs_root / f"demo-oc-agg{k}" / TASK_ID

    def events(self, k=1):
        path = self.task_dir(k) / "trace.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def summary(self, k=1):
        return json.loads((self.task_dir(k) / "summary.json").read_text())

    def parents(self, events):
        out = {}
        for event in events:
            out[event["span_id"]] = event["parent_id"]
        return out

    def root_of(self, span_id, parents):
        seen = set()
        while parents.get(span_id) is not None:
            self.assertNotIn(span_id, seen, "a span cycle")
            seen.add(span_id)
            span_id = parents[span_id]
        return span_id

    def libraries(self):
        surface = self.adapter.substrate.surface()
        return {
            mode: memory.connector_for(
                self.root / "lib" / mode, self.adapter.domain_of(TASK_ID), surface
            )
            for mode in schedule.MODES
        }

    def run_with_memory(self, k=1):
        return schedule.run_task(
            TASK_ID,
            k,
            adapter=self.adapter,
            prop=self.proposer,
            runs_root=self.adapter.spec.runs_root,
            libraries=self.libraries(),
            memory_recall=True,
        )

    def mode_spans(self, events):
        """span_id -> mode for every MODE start event."""
        return {
            event["span_id"]: event["attrs"]["mode"]
            for event in events
            if event["kind"] == trace.Kind.MODE and event["attrs"].get("phase") == "start"
        }

    def assert_spans_are_paired(self, events):
        phases = {}
        for event in events:
            phase = event["attrs"].get("phase")
            if phase is not None:
                phases.setdefault(event["span_id"], set()).add(phase)
        for span_id, got in phases.items():
            self.assertEqual(got, {"start", "end"}, f"span {span_id} is unpaired: {got}")

    def test_trace_file_links_every_span_and_records_the_summary(self):
        result = self.run_one()
        self.assertEqual(result.status, "ok")
        path = self.task_dir() / "trace.jsonl"
        self.assertTrue(path.is_file())
        events = self.events()
        self.assertTrue(events)

        self.assertEqual(len({event["run_id"] for event in events}), 1)
        self.assertFalse([e for e in events if e["kind"].startswith(trace.JUDGE_PREFIX)])
        parents = self.parents(events)
        task_starts = [
            event
            for event in events
            if event["kind"] == trace.Kind.TASK and event["attrs"].get("phase") == "start"
        ]
        self.assertEqual(len(task_starts), 1)
        task_id = task_starts[0]["span_id"]
        self.assertIsNone(task_starts[0]["parent_id"])

        roles = {
            event["attrs"].get("role")
            for event in events
            if event["kind"] == trace.Kind.ROLLOUT and event["attrs"].get("phase") == "start"
        }
        self.assertEqual(roles, {"base", "candidate", "final"})
        for kind in (
            trace.Kind.ROLLOUT,
            trace.Kind.STAGE,
            trace.Kind.SELECTION,
            trace.Kind.COST,
        ):
            kind_events = [event for event in events if event["kind"] == kind]
            self.assertTrue(kind_events, f"no {kind} events")
            for event in kind_events:
                self.assertEqual(self.root_of(event["span_id"], parents), task_id)

        stage_ids = {
            event["span_id"]
            for event in events
            if event["kind"] == trace.Kind.STAGE and event["attrs"].get("phase") == "start"
        }
        diffs = [event for event in events if event["kind"] == trace.Kind.TREE_DIFF]
        self.assertTrue(diffs)
        for diff in diffs:
            self.assertIn(diff["parent_id"], stage_ids)
            for entry in diff["attrs"]["files"]:
                self.assertEqual(set(entry), {"path", "sha256", "change"})
                self.assertIn(entry["change"], {"added", "changed", "removed"})
                self.assertEqual(len(entry["sha256"]), 64)

        self.assert_spans_are_paired(events)
        self.assertFalse(
            [event for event in events if event["kind"].startswith(trace.JUDGE_PREFIX)]
        )

        summary = self.summary()
        recorded = summary["trace"]
        self.assertEqual(set(recorded), {"enabled", "sink", "run_id", "events", "warnings"})
        self.assertTrue(recorded["enabled"])
        self.assertEqual(recorded["sink"], "file")
        self.assertEqual(recorded["warnings"], [])
        same_run = [event for event in events if event["run_id"] == recorded["run_id"]]
        self.assertEqual(recorded["events"], len(same_run))
        self.assertEqual(set(event["run_id"] for event in same_run), {recorded["run_id"]})

    def test_pooled_stages_and_rollouts_hang_from_the_mode_span(self):
        result = self.run_with_memory(k=2)
        self.assertEqual(result.status, "ok")
        events = self.events(k=2)
        parents = self.parents(events)
        starts = [event for event in events if event["attrs"].get("phase") == "start"]
        task_starts = [event for event in starts if event["kind"] == trace.Kind.TASK]
        self.assertEqual(len(task_starts), 1)
        task_id = task_starts[0]["span_id"]
        mode_ids = self.mode_spans(events)
        self.assertTrue(mode_ids)
        for span_id, mode in mode_ids.items():
            self.assertEqual(parents[span_id], task_id, "a MODE span must hang from TASK")
            self.assertIn(mode, schedule.MODES)

        for kind in (
            trace.Kind.STAGE,
            trace.Kind.SELECTION,
            trace.Kind.REFLECTION,
        ):
            found = [event for event in starts if event["kind"] == kind]
            self.assertTrue(found, f"no {kind} events")
            for event in found:
                self.assertIn(event["parent_id"], mode_ids, f"{kind} must hang from MODE")
        for kind in (trace.Kind.COST, trace.Kind.MEMORY_RECALL, trace.Kind.MEMORY_STORE):
            found = [event for event in events if event["kind"] == kind]
            self.assertTrue(found, f"no {kind} events")
            for event in found:
                self.assertIn(event["parent_id"], mode_ids, f"{kind} must hang from MODE")
                self.assertEqual(event["attrs"]["mode"], mode_ids[event["parent_id"]])

        base_rollouts = [
            event
            for event in starts
            if event["kind"] == trace.Kind.ROLLOUT and event["attrs"].get("role") == "base"
        ]
        self.assertTrue(base_rollouts)
        for event in base_rollouts:
            self.assertEqual(event["parent_id"], task_id, "a base ROLLOUT must hang from TASK")
        mode_rollouts = [
            event
            for event in starts
            if event["kind"] == trace.Kind.ROLLOUT
            and event["attrs"].get("role") in ("candidate", "final")
        ]
        self.assertTrue(mode_rollouts)
        for event in mode_rollouts:
            self.assertIn(
                event["parent_id"], mode_ids, "a candidate/final ROLLOUT must hang from MODE"
            )
            self.assertEqual(event["attrs"]["mode"], mode_ids[event["parent_id"]])
        for event in events:
            self.assertEqual(self.root_of(event["span_id"], parents), task_id)
        self.assert_spans_are_paired(events)

    def test_one_mode_span_per_mode_with_its_mode(self):
        self.run_one()
        events = self.events()
        starts = [
            event
            for event in events
            if event["kind"] == trace.Kind.MODE and event["attrs"].get("phase") == "start"
        ]
        ends = [
            event
            for event in events
            if event["kind"] == trace.Kind.MODE and event["attrs"].get("phase") == "end"
        ]
        self.assertEqual(sorted(event["attrs"]["mode"] for event in starts), sorted(schedule.MODES))
        self.assertEqual(
            sorted(event["span_id"] for event in starts),
            sorted(event["span_id"] for event in ends),
        )
        self.assertEqual({event["run_id"] for event in starts}, {event["run_id"] for event in ends})
        for event in starts:
            self.assertIn(event["attrs"]["mode"], schedule.MODES)
        for event in ends:
            self.assertEqual(event["attrs"]["status"], "ok")

    def test_disabled_tracing_writes_no_file_and_says_so(self):
        with patch.dict(os.environ, {trace.ENABLE_VAR: "0"}):
            result = self.run_one()
        self.assertEqual(result.status, "ok")
        self.assertFalse((self.task_dir() / "trace.jsonl").exists())
        self.assertEqual(self.summary()["trace"], {"enabled": False})
        self.assertEqual(result.trace, {"enabled": False})

    def test_a_failing_sink_does_not_stop_the_run_and_leaves_a_warning(self):
        with patch.dict(os.environ, {trace.ENV_VAR: "tests.test_trace_flow:broken_sink"}):
            result = self.run_one()
        self.assertEqual(result.status, "ok")
        recorded = self.summary()["trace"]
        self.assertTrue(recorded["enabled"])
        self.assertEqual(recorded["sink"], "tests.test_trace_flow:broken_sink")
        self.assertEqual(recorded["events"], 0)
        self.assertTrue(recorded["warnings"])
        self.assertTrue(recorded["warnings"][0].startswith("emit failed: RuntimeError"))

    def test_resume_appends_a_second_run_and_marks_the_task_resumed(self):
        first = self.run_one()
        first_run = first.trace["run_id"]
        first_summary = self.summary()
        executions = self.adapter.execution_count
        calls = len(self.proposer._calls)

        second = self.run_one()
        self.assertEqual(second.status, "skipped")
        self.assertEqual(
            (self.adapter.execution_count, len(self.proposer._calls)), (executions, calls)
        )
        events = self.events()
        self.assertEqual(len({event["run_id"] for event in events}), 2)
        new = [event for event in events if event["run_id"] != first_run]
        self.assertEqual({event["run_id"] for event in new}, {second.trace["run_id"]})
        task_end = [
            event
            for event in new
            if event["kind"] == trace.Kind.TASK and event["attrs"].get("phase") == "end"
        ]
        self.assertEqual(len(task_end), 1)
        self.assertTrue(task_end[0]["attrs"]["resumed"])

        task_starts = [
            event
            for event in events
            if event["kind"] == trace.Kind.TASK and event["attrs"].get("phase") == "start"
        ]
        self.assertEqual(len(task_starts), 2)
        for event in task_starts:
            self.assertNotIn("resumed", event["attrs"])
        task_ends = {
            event["run_id"]: event["attrs"]
            for event in events
            if event["kind"] == trace.Kind.TASK and event["attrs"].get("phase") == "end"
        }
        self.assertFalse(task_ends[first_run]["resumed"])
        self.assertTrue(task_ends[second.trace["run_id"]]["resumed"])

        summary = self.summary()
        self.assertEqual(summary["trace"]["run_id"], second.trace["run_id"])
        self.assertEqual(summary["status"], "ok")
        for key in ("status", "modes", "selections", "errors"):
            self.assertEqual(summary[key], first_summary[key], f"resume changed {key}")

        third = self.run_one()
        self.assertEqual(third.status, "skipped")
        self.assertEqual(
            (self.adapter.execution_count, len(self.proposer._calls)), (executions, calls)
        )
        self.assertEqual(self.summary()["trace"]["run_id"], third.trace["run_id"])

    def test_resume_wraps_each_mode_write_back_in_a_resumed_mode_span(self):
        first = self.run_with_memory()
        self.assertEqual(first.status, "ok")
        second = self.run_with_memory()
        self.assertEqual(second.status, "skipped")
        events = [event for event in self.events() if event["run_id"] == second.trace["run_id"]]
        mode_ids = self.mode_spans(events)
        self.assertEqual(sorted(mode_ids.values()), sorted(schedule.MODES))
        mode_starts = [
            event
            for event in events
            if event["kind"] == trace.Kind.MODE and event["attrs"].get("phase") == "start"
        ]
        for event in mode_starts:
            self.assertIs(event["attrs"]["resumed"], True)
        stores = [event for event in events if event["kind"] == trace.Kind.MEMORY_STORE]
        self.assertTrue(stores)
        for event in stores:
            self.assertIn(
                event["parent_id"], mode_ids, "a resumed MEMORY_STORE must hang from MODE"
            )
            self.assertEqual(event["attrs"]["mode"], mode_ids[event["parent_id"]])

    def test_write_trace_leaves_a_missing_summary_alone(self):
        work = self.task_dir()
        work.mkdir(parents=True, exist_ok=True)
        summary_path = work / "summary.json"
        schedule._write_trace(summary_path, {"enabled": True, "run_id": "run"})
        self.assertFalse(summary_path.exists())

    def candidate(self, work, label="agg1"):
        run_dir = work / "runs" / label
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "trajectory.md").write_text(
            "[ACTION] run scripts/summarize.py\n[OBSERVATION]\nTotal: 85.15\n", encoding="utf-8"
        )
        (run_dir / "answer.txt").write_text("Total: 85.15\n", encoding="utf-8")
        return RunView(label=label, run_id=f"run-{label}", run_dir=run_dir, ok=True, delivered=True)

    def candidates_for(self, work, label="agg1"):
        harness = work / "harnesses" / label
        harness.mkdir(parents=True, exist_ok=True)
        (harness / "systemprompt.md").write_text("Reconcile the ledger.\n", encoding="utf-8")
        return {label: self.candidate(work, label)}

    def rollout_events(self, adapter, pending):
        sink = RecordingSink()
        tracer = trace.Tracer(sink)
        self.addCleanup(tracer.close)
        with trace.use(tracer):
            schedule._roll_bases(TASK_ID, adapter, pending, self.root / "missing")
        return [event for event in sink.events if event.kind == trace.Kind.ROLLOUT]

    def test_reflection_traces_only_its_own_cost_and_failure(self):
        for fails in (False, True):
            with self.subTest(reflect_fails=fails):
                work = self.root / f"cost-{fails}"
                candidates = self.candidates_for(work)
                proposer = CostProposer(reflect_fails=fails)
                sink = RecordingSink()
                tracer = trace.Tracer(sink)
                self.addCleanup(tracer.close)
                with trace.use(tracer):
                    sel = selection.run_selection(
                        TASK_ID,
                        1,
                        "agg",
                        candidates,
                        adapter=self.adapter,
                        prop=proposer,
                        work_dir=work,
                    )
                    experience = selection.reflect_adoption(
                        TASK_ID,
                        sel,
                        candidates,
                        adapter=self.adapter,
                        prop=proposer,
                        work_dir=work,
                    )
                ends = {
                    event.kind: event.attrs
                    for event in sink.events
                    if event.attrs.get("phase") == "end"
                }
                selected = ends[trace.Kind.SELECTION]
                reflected = ends[trace.Kind.REFLECTION]
                self.assertEqual(selected["usd"], proposer.select_usd)
                self.assertAlmostEqual(reflected["usd"], proposer.reflect_usd)
                self.assertAlmostEqual(selected["usd"] + reflected["usd"], proposer.spent)
                self.assertEqual(reflected["ok"], not fails)
                self.assertEqual(bool(experience), not fails)
                self.assertEqual(reflected["error"] == "", not fails)

    def test_a_failed_batch_emits_one_rollout_per_run_id(self):
        first = self.root / "batch-r1"
        second = self.root / "batch-r2"
        first.mkdir()
        second.mkdir()
        events = self.rollout_events(
            BatchAdapter([first, second], fail_label="batch-r2"),
            [("base1", "r1"), ("base2", "r2")],
        )
        starts = [event for event in events if event.attrs.get("phase") == "start"]
        ends = [event for event in events if event.attrs.get("phase") == "end"]
        self.assertEqual(sorted(event.attrs["run_id"] for event in starts), ["r1", "r2"])
        self.assertEqual(
            sorted(event.span_id for event in starts),
            sorted(event.span_id for event in ends),
        )
        starts_by_id = {event.span_id: event.attrs for event in starts}
        ends_by_id = {event.span_id: event.attrs for event in ends}
        by_run_id = {
            starts_by_id[span_id]["run_id"]: ends_by_id[span_id] for span_id in starts_by_id
        }
        self.assertTrue(by_run_id["r1"]["ok"])
        self.assertEqual(by_run_id["r1"]["error"], "")
        self.assertFalse(by_run_id["r2"]["ok"])
        self.assertEqual(by_run_id["r2"]["error"], "RuntimeError: delivered failed")

    def test_both_rollout_paths_trace_the_same_attrs(self):
        batch_first = self.root / "same-r1"
        batch_second = self.root / "same-r2"
        single = self.root / "single-r1"
        for path in (batch_first, batch_second, single):
            path.mkdir()
        batch = self.rollout_events(
            BatchAdapter([batch_first, batch_second]), [("base1", "r1"), ("base2", "r2")]
        )
        one = self.rollout_events(SingleAdapter([single]), [("base1", "r1")])
        batch_start = next(event for event in batch if event.attrs.get("phase") == "start")
        batch_end = next(event for event in batch if event.attrs.get("phase") == "end")
        one_start = next(event for event in one if event.attrs.get("phase") == "start")
        one_end = next(event for event in one if event.attrs.get("phase") == "end")
        self.assertEqual(set(batch_start.attrs), set(one_start.attrs))
        self.assertEqual(set(batch_end.attrs), set(one_end.attrs))
        self.assertEqual(batch_start.attrs["mode"], "")
        self.assertEqual(one_start.attrs["mode"], "")
        self.assertEqual(
            set(one_start.attrs) | set(one_end.attrs),
            {
                "role",
                "mode",
                "label",
                "run_id",
                "phase",
                "status",
                "dur_s",
                "ok",
                "delivered",
                "usd",
                "error",
            },
        )

    def test_a_single_base_rollout_span_wraps_the_execution(self):
        real = self.root / "wrapped"
        real.mkdir()
        sink = RecordingSink()
        tracer = trace.Tracer(sink)
        self.addCleanup(tracer.close)
        with trace.use(tracer):
            schedule._roll_bases(
                TASK_ID,
                EmittingDirectoryCostAdapter(real),
                [("base1", "r1")],
                self.root / "missing",
            )
        route = next(event for event in sink.events if event.kind == trace.Kind.MODEL_ROUTE)
        start = next(
            event
            for event in sink.events
            if event.kind == trace.Kind.ROLLOUT and event.attrs.get("phase") == "start"
        )
        end = next(
            event
            for event in sink.events
            if event.kind == trace.Kind.ROLLOUT and event.attrs.get("phase") == "end"
        )
        self.assertEqual(route.parent_id, start.span_id)
        self.assertGreater(end.attrs["dur_s"], 0.0)
        self.assertTrue(end.attrs["ok"])
        self.assertEqual(end.attrs["usd"], 1.0)

    def test_a_single_base_on_a_missing_directory_is_not_ok(self):
        missing = self.root / "not-there"
        adapter = DirectoryCostAdapter(missing)
        rolled = schedule._roll_bases(TASK_ID, adapter, [("base1", "r1")], self.root / "missing")
        rv = rolled["base1"]
        self.assertFalse(rv.ok)
        self.assertFalse(rv.delivered)
        self.assertEqual(rv.error, "adapter returned a missing run directory")
        self.assertEqual(rv.cost_usd, 0.0)
        self.assertEqual(adapter.cost_dirs, [missing])

    def test_a_single_base_failure_is_priced_from_the_returned_directory(self):
        real = self.root / "priced"
        real.mkdir()
        adapter = DirectoryCostAdapter(real, delivered_raises=True)
        rolled = schedule._roll_bases(TASK_ID, adapter, [("base1", "r1")], self.root / "missing")
        rv = rolled["base1"]
        self.assertFalse(rv.ok)
        self.assertFalse(rv.delivered)
        self.assertEqual(rv.run_dir, self.root / "missing")
        self.assertEqual(rv.error, "RuntimeError: delivered failed")
        self.assertEqual(rv.cost_usd, 1.0)
        self.assertEqual(adapter.cost_dirs, [real])

    def test_a_recursion_rollout_failure_is_priced_from_the_returned_directory(self):
        real = self.root / "priced-recursion"
        real.mkdir()
        adapter = DirectoryCostAdapter(real, delivered_raises=True)
        ledger = Ledger("agg", TASK_ID, 1)
        rv = recursion._roll(
            adapter, ledger, TASK_ID, "c1", None, lambda label: f"rid-{label}", self.root
        )
        self.assertFalse(rv.ok)
        self.assertEqual(rv.run_dir, self.root / "missing")
        self.assertEqual(rv.error, "RuntimeError: delivered failed")
        self.assertEqual(rv.cost_usd, 1.0)
        self.assertEqual(ledger.rollout_usd, 1.0)
        self.assertEqual(adapter.cost_dirs, [real])

    def test_a_failed_batch_item_keeps_its_siblings_and_its_cost(self):
        first = self.root / "item-r1"
        second = self.root / "item-r2"
        first.mkdir()
        second.mkdir()
        adapter = BatchAdapter([first, second], fail_label="item-r2")
        rolled = schedule._roll_bases(
            TASK_ID, adapter, [("base1", "r1"), ("base2", "r2")], self.root / "missing"
        )
        self.assertTrue(rolled["base1"].ok)
        self.assertTrue(rolled["base1"].delivered)
        self.assertEqual(rolled["base1"].cost_usd, 1.0)
        self.assertFalse(rolled["base2"].ok)
        self.assertFalse(rolled["base2"].delivered)
        self.assertEqual(rolled["base2"].error, "RuntimeError: delivered failed")
        self.assertEqual(rolled["base2"].run_dir, self.root / "missing")
        self.assertEqual(rolled["base2"].cost_usd, 1.0)
        self.assertEqual(adapter.cost_dirs, [first, second])

    def test_a_mode_failure_keeps_its_ledger_and_still_emits_cost(self):
        adapter = CostDemoAdapter(self.root)
        proposer = DemoProposer(adapter.spec.channels["proposer"])
        result = schedule.run_task(
            TASK_ID,
            2,
            adapter=adapter,
            prop=proposer,
            runs_root=adapter.spec.runs_root,
            modes=("agg",),
            selector_prop=ExplodingSelector(proposer),
        )
        self.assertEqual(result.status, "partial")
        record = result.modes["agg"]
        self.assertFalse(record["ok"])
        self.assertEqual(record["error"], "RuntimeError: selector exploded")
        self.assertTrue(record["traceback"])
        self.assertEqual(record["ledger"]["usd"], 4.0)
        costs = [
            event
            for event in self.events(2)
            if event["kind"] == trace.Kind.COST and event["attrs"]["mode"] == "agg"
        ]
        self.assertEqual(len(costs), 1)
        self.assertEqual(costs[0]["attrs"]["usd"], record["ledger"]["usd"])
        self.assertEqual(costs[0]["attrs"]["R"], record["ledger"]["R"])
        mode_ends = [
            event
            for event in self.events(2)
            if event["kind"] == trace.Kind.MODE and event["attrs"].get("phase") == "end"
        ]
        self.assertEqual(len(mode_ends), 1)
        self.assertIs(mode_ends[0]["attrs"]["ok"], False)
        self.assertEqual(mode_ends[0]["attrs"]["error"], "agg: RuntimeError: selector exploded")

    def test_a_midrun_summary_keeps_the_previous_trace(self):
        work = self.task_dir()
        work.mkdir(parents=True, exist_ok=True)
        summary_path = work / "summary.json"
        old = {"enabled": True, "sink": "file", "run_id": "old", "events": 3, "warnings": []}
        summary_path.write_text(json.dumps({"status": "running", "modes": {}, "trace": old}))
        res = schedule.TaskResult(task_id=TASK_ID, domain="accounting", k=1)
        schedule._write_summary(summary_path, res)
        self.assertEqual(json.loads(summary_path.read_text())["trace"], old)

        res.trace = {
            "enabled": True,
            "sink": "file",
            "run_id": "new",
            "events": 5,
            "warnings": [],
        }
        schedule._write_summary(summary_path, res)
        self.assertEqual(json.loads(summary_path.read_text())["trace"], res.trace)

    def test_a_failed_reflection_still_reports_its_cost(self):
        work = self.root / "reflection-failure"
        candidates = self.candidates_for(work)
        sel = selection.Selection(task_id=TASK_ID, mode="agg", k=1, ok=True, chosen_label="agg1")

        def explode(task_id, selection_record, *args, **kwargs):
            selection_record.usd += 0.25
            raise RuntimeError("reflection exploded")

        sink = RecordingSink()
        tracer = trace.Tracer(sink)
        self.addCleanup(tracer.close)
        with trace.use(tracer):
            with patch.object(selection, "_reflect_adoption", side_effect=explode):
                with self.assertRaises(RuntimeError):
                    selection.reflect_adoption(
                        TASK_ID,
                        sel,
                        candidates,
                        adapter=self.adapter,
                        prop=object(),
                        work_dir=work,
                    )
        reflected = [
            event
            for event in sink.events
            if event.kind == trace.Kind.REFLECTION and event.attrs.get("phase") == "end"
        ]
        self.assertEqual(len(reflected), 1)
        self.assertEqual(reflected[0].attrs["usd"], 0.25)
        self.assertEqual(reflected[0].attrs["status"], "error")


class ReflectionProposer:
    """Answers the selector, then fails or answers the reflection on demand."""

    def __init__(self, *, fail=None, text=EXPERIENCE_TEXT):
        self.fail = fail
        self.text = text

    def call(self, mission, context_dir, label, **kwargs):
        if label.startswith("select."):
            return Reply(label=label, text="```choice\nA\n```", ok=True, cost_usd=0.0)
        if self.fail is not None:
            raise ProposerError(self.fail, cost_usd=0.1)
        return Reply(label=label, text=self.text, ok=True, cost_usd=0.1)


class ReflectionErrorTests(unittest.TestCase):
    """The REFLECTION error says why the reflection gave no experience."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adapter = DemoAdapter(self.root)

    def candidates_for(self, work, label="agg1"):
        run_dir = work / "runs" / label
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "trajectory.md").write_text(
            "[ACTION] run scripts/summarize.py\n[OBSERVATION]\nTotal: 85.15\n", encoding="utf-8"
        )
        (run_dir / "answer.txt").write_text("Total: 85.15\n", encoding="utf-8")
        harness = work / "harnesses" / label
        harness.mkdir(parents=True, exist_ok=True)
        (harness / "systemprompt.md").write_text("Reconcile the ledger.\n", encoding="utf-8")
        return {
            label: RunView(
                label=label, run_id=f"run-{label}", run_dir=run_dir, ok=True, delivered=True
            )
        }

    def reflect(self, proposer, work):
        candidates = self.candidates_for(work)
        sink = RecordingSink()
        tracer = trace.Tracer(sink)
        self.addCleanup(tracer.close)
        with trace.use(tracer):
            sel = selection.run_selection(
                TASK_ID,
                1,
                "agg",
                candidates,
                adapter=self.adapter,
                prop=proposer,
                work_dir=work,
            )
            experience = selection.reflect_adoption(
                TASK_ID,
                sel,
                candidates,
                adapter=self.adapter,
                prop=proposer,
                work_dir=work,
            )
        reflected = [
            event
            for event in sink.events
            if event.kind == trace.Kind.REFLECTION and event.attrs.get("phase") == "end"
        ]
        self.assertEqual(len(reflected), 1)
        return experience, reflected[0].attrs

    def test_a_proposer_error_becomes_the_reflection_error(self):
        experience, attrs = self.reflect(ReflectionProposer(fail="boom"), self.root / "failed")
        self.assertEqual(experience, {})
        self.assertFalse(attrs["ok"])
        self.assertAlmostEqual(attrs["usd"], 0.1)
        self.assertTrue(attrs["error"].startswith("ProposerError"), attrs["error"])
        self.assertIn("boom", attrs["error"])

    def test_a_reply_without_an_experience_explains_itself(self):
        proposer = ReflectionProposer(text="I found nothing worth keeping.\n")
        experience, attrs = self.reflect(proposer, self.root / "empty")
        self.assertEqual(experience, {})
        self.assertFalse(attrs["ok"])
        self.assertEqual(attrs["error"], "no experience in the reply")

    def test_a_successful_reflection_has_no_error(self):
        experience, attrs = self.reflect(ReflectionProposer(), self.root / "ok")
        self.assertTrue(experience)
        self.assertTrue(attrs["ok"])
        self.assertEqual(attrs["error"], "")


if __name__ == "__main__":
    unittest.main()
