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

"""Tests for `harness2.core.trace_source`: the lookup, the built-in `trace` and the rules."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness2.core import evaluate, trace, trace_file, trace_source
from harness2.core.trace_file import FILE_NAME, FileSink
from harness2.core.trace_source import Origin, SourcedEvent, SourceRead

TS = "2026-01-01T00:00:00+00:00"


def sourced(kind, span_id, *, run_id="x1", parent_id=None, line=1, **attrs):
    """One event of an external run, read from line `line` of `records.jsonl`."""
    return SourcedEvent(
        trace.TraceEvent(
            run_id=run_id,
            span_id=span_id,
            parent_id=parent_id,
            kind=kind,
            ts=TS,
            attrs=attrs,
        ),
        Origin("orchestrator", Path("records.jsonl"), line),
    )


class EmptySource:
    """A trace source that reads nothing."""

    def __init__(self, **options):
        self.options = options

    def read(self, location):
        return SourceRead([], [])


class FakeEntryPoint:
    """An installed entry point whose `load()` returns `target`."""

    def __init__(self, name, target):
        self.name = name
        self.target = target

    def load(self):
        return self.target


class LookupTests(unittest.TestCase):
    def setUp(self):
        self._registry = dict(trace_source._REGISTRY)
        self.addCleanup(self._restore)

    def _restore(self):
        trace_source._REGISTRY.clear()
        trace_source._REGISTRY.update(self._registry)

    def test_trace_is_built_in(self):
        self.assertIn(trace_source.TRACE, trace_source.names())
        self.assertIsInstance(
            trace_source.trace_source_for(trace_source.TRACE), trace_source.TraceFiles
        )

    def test_a_registration_wins_over_an_entry_point_of_the_same_name(self):
        trace_source.register("fake", EmptySource)
        other = FakeEntryPoint("fake", lambda **options: object())
        with mock.patch.object(evaluate, "_entry_points", return_value=[other]):
            self.assertIs(trace_source.resolve_factory("fake"), EmptySource)

    def test_an_entry_point_of_the_trace_source_group_is_found(self):
        groups = []

        def entry_points(group):
            groups.append(group)
            return [FakeEntryPoint("installed", EmptySource)]

        with mock.patch.object(evaluate, "_entry_points", side_effect=entry_points):
            source = trace_source.trace_source_for("installed", options={"a": 1})
            self.assertIn("installed", trace_source.names())
        self.assertIsInstance(source, EmptySource)
        self.assertEqual(source.options, {"a": 1})
        self.assertEqual(set(groups), {trace_source.ENTRY_POINT_GROUP})

    def test_a_module_attr_path_is_imported(self):
        source = trace_source.trace_source_for(f"{__name__}:EmptySource")
        self.assertIsInstance(source, EmptySource)

    def test_an_unknown_name_raises_value_error(self):
        with self.assertRaises(ValueError) as caught:
            trace_source.trace_source_for("nope")
        self.assertIn("nope", str(caught.exception))
        self.assertIn(trace_source.ENTRY_POINT_GROUP, str(caught.exception))

    def test_a_factory_that_returns_no_trace_source_raises_type_error(self):
        trace_source.register("broken", lambda **options: object())
        with self.assertRaises(TypeError):
            trace_source.trace_source_for("broken")

    def test_read_from_rejects_a_result_that_is_not_a_pair(self):
        class Bad:
            def read(self, location):
                return 42

        with self.assertRaises(TypeError):
            trace_source.read_from(Bad(), "x")
        result = trace_source.read_from(EmptySource(), "x")
        self.assertEqual(result, SourceRead([], []))


class TraceFilesTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def write_run(self, folder, run_id):
        task_dir = self.root / folder
        tracer = trace.Tracer(FileSink(task_dir), run_id=run_id)
        with trace.use(tracer):
            with trace.span(trace.Kind.TASK, task_id=run_id):
                trace.event(trace.Kind.COST, usd=0.1)
        tracer.close()
        return task_dir / FILE_NAME

    def test_reads_the_same_events_as_the_trace_file_reader_with_their_lines(self):
        first = self.write_run("a", "r1")
        second = self.write_run("b/c", "r2")
        with first.open("a", encoding="utf-8") as handle:
            handle.write("not json\n\n")
        self.write_run("a", "r3")

        result = trace_source.TraceFiles().read(str(self.root))
        self.assertEqual(result.warnings, [])
        expected = trace_file.read(first) + trace_file.read(second)
        self.assertEqual([item.event for item in result.events], expected)
        self.assertEqual({item.origin.name for item in result.events}, {trace_source.TRACE})
        lines = [item.origin.line for item in result.events if item.origin.file == first]
        self.assertEqual(lines, [1, 2, 3, 6, 7, 8])
        self.assertEqual(trace_source.trace_paths(self.root, self.root), [first, second])

    def test_an_unreadable_trace_is_a_warning(self):
        good = self.write_run("good", "r1")
        bad = self.write_run("bad", "r2")
        if os.geteuid() == 0:
            self.skipTest("file permissions do not stop root")
        bad.chmod(0o000)
        self.addCleanup(bad.chmod, 0o644)

        result = trace_source.TraceFiles().read(str(self.root))
        self.assertEqual({item.origin.file for item in result.events}, {good})
        self.assertEqual(len(result.warnings), 1)
        self.assertTrue(result.warnings[0].startswith(f"cannot read {bad}"))


class CheckTests(unittest.TestCase):
    def test_a_well_formed_external_run_is_kept(self):
        events = [
            sourced("task", "t", phase="start", system="orchestrator"),
            sourced("orchestrator.question.asked", "q", parent_id="t", line=2),
            sourced("task", "t", phase="end", status="ok", line=3),
        ]
        kept, warnings = trace_source.check(events)
        self.assertEqual(kept, events)
        self.assertEqual(warnings, [])

    def test_a_task_without_a_system_is_skipped_with_a_warning(self):
        kept, warnings = trace_source.check([sourced("task", "t")])
        self.assertEqual(kept, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("system", warnings[0])
        self.assertIn("records.jsonl:1", warnings[0])

    def test_a_task_that_names_harness2_is_skipped(self):
        kept, warnings = trace_source.check([sourced("task", "t", system="harness2")])
        self.assertEqual(kept, [])
        self.assertEqual(len(warnings), 1)

    def test_a_kind_without_the_namespace_is_skipped(self):
        events = [
            sourced("task", "t", system="orchestrator"),
            sourced("question.asked", "q", parent_id="t", line=2),
            sourced("orchestrator.", "e", parent_id="t", line=3),
            sourced("other.question", "o", parent_id="t", line=4),
        ]
        kept, warnings = trace_source.check(events)
        self.assertEqual(kept, events[:1])
        self.assertEqual(len(warnings), 3)
        self.assertTrue(all("namespace" in warning for warning in warnings))

    def test_a_built_in_kind_is_skipped(self):
        events = [sourced("task", "t", system="orchestrator")]
        for line, kind in enumerate(
            (trace.Kind.MODE, trace.Kind.ROLLOUT, trace.Kind.ERROR, "judge.score"), start=2
        ):
            events.append(sourced(kind, f"s{line}", parent_id="t", line=line))
        kept, warnings = trace_source.check(events)
        self.assertEqual(kept, events[:1])
        self.assertEqual(len(warnings), 4)
        self.assertTrue(all("built-in kind" in warning for warning in warnings))

    def test_an_event_of_a_run_without_a_task_is_skipped(self):
        kept, warnings = trace_source.check([sourced("orchestrator.step", "s")])
        self.assertEqual(kept, [])
        self.assertEqual(len(warnings), 1)

    def test_an_item_that_is_not_a_sourced_event_is_a_warning(self):
        broken = SourcedEvent(
            trace.TraceEvent(run_id="", span_id="s", parent_id=None, kind="task", ts=TS),
            Origin("orchestrator", "records.jsonl", 9),
        )
        kept, warnings = trace_source.check(["nope", broken])
        self.assertEqual(kept, [])
        self.assertEqual(len(warnings), 2)
        self.assertIn("records.jsonl:9", warnings[1])


if __name__ == "__main__":
    unittest.main()
