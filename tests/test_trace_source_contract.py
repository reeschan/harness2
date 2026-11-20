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

"""Tests for `harness2.testing.trace_source_contract`: the fake trace source and the checks."""

import unittest
from pathlib import Path

from harness2.core import trace, trace_source
from harness2.core.trace_source import Origin, SourcedEvent, SourceRead
from harness2.testing.trace_source_contract import (
    FixedTraceSource,
    TraceSourceContractTests,
    check_trace_source,
    contract_events,
)

TS = "2026-01-01T00:00:00+00:00"


def sourced(kind, span_id, *, run_id="x1", parent_id=None, line=1, name="fixed", **attrs):
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
        Origin(name, Path("records.jsonl"), line),
    )


def run(system="orchestrator", run_id="x1"):
    """A root task naming `system` and one namespaced point event inside it."""
    return [
        sourced("task", "t", run_id=run_id, system=system),
        sourced(f"{system}.step", "s", run_id=run_id, parent_id="t", line=2),
    ]


class ContractEventsTests(unittest.TestCase):
    def test_events_form_one_run_with_a_root_task_naming_the_system(self):
        events = contract_events(system="pipeline", name="mine")
        kept, warnings = trace_source.check(events)
        self.assertEqual(warnings, [])
        self.assertEqual(len(kept), len(events))
        roots = [item.event for item in events if item.event.parent_id is None]
        self.assertTrue(all(event.kind == trace.Kind.TASK for event in roots))
        self.assertEqual(roots[0].attrs[trace_source.SYSTEM_ATTR], "pipeline")
        self.assertEqual({item.origin.name for item in events}, {"mine"})
        self.assertEqual([item.origin.line for item in events], [1, 2, 3, 4, 5])
        self.assertTrue(
            all(item.event.kind.startswith("pipeline.") for item in events if item.event.parent_id)
        )


class FixedTraceSourceTests(unittest.TestCase):
    def test_it_is_a_trace_source(self):
        self.assertIsInstance(FixedTraceSource(), trace_source.TraceSource)

    def test_read_returns_new_lists_of_what_it_was_given(self):
        events = run()
        source = FixedTraceSource(events, ["line 9 is broken"])
        first = source.read("anywhere")
        second = source.read("elsewhere")
        self.assertEqual(first, SourceRead(events, ["line 9 is broken"]))
        self.assertIsNot(first.events, second.events)

    def test_events_default_to_the_contract_events(self):
        self.assertEqual(FixedTraceSource().read("x").events, contract_events())


class FixedTraceSourceContractTest(TraceSourceContractTests, unittest.TestCase):
    trace_source_name = "fixed"

    def make_location(self, root):
        return str(root)

    def make_trace_source(self):
        return FixedTraceSource()


class TwoRunsContractTest(TraceSourceContractTests, unittest.TestCase):
    def make_location(self, root):
        return str(root)

    def make_trace_source(self):
        return FixedTraceSource(run("orchestrator", "x1") + run("pipeline", "x2"))


class ContractFailureTests(unittest.TestCase):
    def assert_fails(self, source, *needles, name=None):
        with self.assertRaises(AssertionError) as caught:
            check_trace_source(source, "here", name=name)
        for needle in needles:
            self.assertIn(needle, str(caught.exception))

    def test_a_well_formed_source_passes_and_returns_what_it_read(self):
        result = check_trace_source(FixedTraceSource(run()), "here", name="fixed")
        self.assertEqual(result, SourceRead(run(), []))

    def test_a_task_without_a_system_fails(self):
        events = [
            sourced("task", "t"),
            sourced("orchestrator.step", "s", parent_id="t", line=2),
        ]
        self.assert_fails(FixedTraceSource(events), "names no system")

    def test_a_kind_without_the_namespace_fails(self):
        events = [*run(), sourced("step", "u", parent_id="t", line=3)]
        self.assert_fails(FixedTraceSource(events), "lacks the namespace 'orchestrator.'")

    def test_a_built_in_kind_fails(self):
        events = [*run(), sourced(trace.Kind.ROLLOUT, "r", parent_id="t", line=3)]
        self.assert_fails(FixedTraceSource(events), "built-in kind")

    def test_a_task_naming_harness2_fails(self):
        self.assert_fails(FixedTraceSource(run(system="harness2")), "harness2")

    def test_a_run_whose_root_is_not_a_task_fails(self):
        events = [*run(), sourced("orchestrator.loose", "l", line=3)]
        self.assert_fails(FixedTraceSource(events), "run x1", "orchestrator.loose l")

    def test_a_run_with_two_tasks_fails(self):
        events = [
            *run(),
            sourced("task", "t2", parent_id="t", line=3, system="orchestrator"),
        ]
        self.assert_fails(FixedTraceSource(events), "run x1", "task spans [t, t2]")

    def test_an_event_whose_parent_is_missing_fails(self):
        events = [*run(), sourced("orchestrator.step", "o", parent_id="gone", line=3)]
        self.assert_fails(FixedTraceSource(events), "missing parent", "gone")

    def test_an_origin_of_another_trace_source_fails(self):
        self.assert_fails(FixedTraceSource(run()), "'fixed'", "'mine'", name="mine")

    def test_an_origin_without_a_name_fails(self):
        events = [sourced("task", "t", name="", system="orchestrator")]
        self.assert_fails(FixedTraceSource(events), "must name the trace source")

    def test_an_origin_without_a_file_fails(self):
        event = sourced("task", "t", system="orchestrator").event
        events = [SourcedEvent(event, Origin("fixed", Path(), 1))]
        self.assert_fails(FixedTraceSource(events), "must name the file")

    def test_an_origin_line_below_one_fails(self):
        events = [sourced("task", "t", line=0, system="orchestrator")]
        self.assert_fails(FixedTraceSource(events), "line number from 1 up")

    def test_an_item_that_is_not_a_sourced_event_fails(self):
        self.assert_fails(FixedTraceSource([*run(), "nope"]), "not a SourcedEvent")

    def test_reading_nothing_fails(self):
        self.assert_fails(FixedTraceSource([]), "no events")

    def test_a_result_that_is_not_a_pair_fails(self):
        class NotAPair:
            def read(self, location):
                return 42

        self.assert_fails(NotAPair(), "expected (events, warnings)")

    def test_an_object_that_is_not_a_trace_source_fails(self):
        self.assert_fails(object(), "is not a TraceSource")

    def test_the_mixin_is_not_a_test_case(self):
        self.assertFalse(issubclass(TraceSourceContractTests, unittest.TestCase))


if __name__ == "__main__":
    unittest.main()
