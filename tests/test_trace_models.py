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

"""The trace events of memory recall/store and of the model candidates."""

import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from harness2.benchmarks import intake_worktree as iw
from harness2.core import memory, models, trace
from harness2.core.proposer import ModelSession, Proposer
from harness2.core.spec import ChannelSpec

CONNECTOR_SPEC = ChannelSpec("connector", "")

OK = models.SessionResult(text="answer", ok=True, cost_usd=0.5)
GONE = models.SessionResult(text="", ok=False, unavailable=True, error="rate limited")
A = models.ModelChoice("claude", "model-a", "high")
B = models.ModelChoice("claude", "model-b", None)


class RecordingSink:
    """Keeps every event it is handed, in order."""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def close(self):
        pass


def choice_attrs(choice):
    return {"runner": choice.runner, "model": choice.model, "effort": choice.effort}


def fence(**changes):
    fields = {
        "capabilities": frozenset({"read", "write", "search"}),
        "workdir_only": True,
        "asks_human": False,
        "harness_credentials": False,
        "runner": "claude",
    }
    fields.update(changes)
    return models.FenceDeclaration(**fields)


class FakeMemory:
    """A connector with an optional `recall` and a recorded `add`."""

    def __init__(self, text="", path=None, recall=True):
        if recall:
            self.recall = self._recall
        self.text = text
        self.path = path
        self.added = []

    def _recall(self, task_id, limit_chars, *, query=""):
        return self.text

    def add(self, task_id, harness, **kwargs):
        self.added.append((task_id, harness, kwargs))
        return self.path


class ImproverConnector:
    """routes + run_session + fences + record, like tests/test_improver_connector.py."""

    def __init__(self, choices, results, fences=None):
        self.choices = list(choices)
        self.results = list(results)
        self.declared = {"claude": fence()} if fences is None else fences
        self.sessions = []

    def routes(self, role, *, task_id, substrate):
        return list(self.choices)

    def fences(self):
        return self.declared

    def run_session(self, role, request):
        self.sessions.append((role, request))
        return self.results.pop(0)

    def record(self, role, task_id, choice, outcome):
        pass


class ExecutorConnector:
    """Only `routes`, which remembers its calls."""

    def __init__(self, choices):
        self.choices = list(choices)
        self.calls = []

    def routes(self, role, *, task_id, substrate):
        self.calls.append((role, task_id, substrate))
        return list(self.choices)


class TraceTestBase(unittest.TestCase):
    def setUp(self):
        self.sink = RecordingSink()
        self.addCleanup(self.sink.close)

    def tracer(self):
        return trace.Tracer(self.sink)

    def events(self, kind):
        return [event for event in self.sink.events if event.kind == kind]

    def one(self, kind):
        found = self.events(kind)
        self.assertEqual(len(found), 1, f"expected one {kind}, got {found}")
        return found[0]


class MemoryTraceTest(TraceTestBase):
    def test_recall_emits_its_length_and_digest_but_not_the_text(self):
        conn = FakeMemory("the lesson body")
        with trace.use(self.tracer()):
            text = memory.recall(conn, "t1", 100)
        self.assertEqual(text, "the lesson body")
        event = self.one(trace.Kind.MEMORY_RECALL)
        self.assertEqual(
            dict(event.attrs),
            {
                "connector": "FakeMemory",
                "task_id": "t1",
                "chars": len(text),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
        )
        self.assertNotIn("the lesson body", json.dumps(dict(event.attrs)))

    def test_recall_without_a_connector_recall_or_a_limit_emits_chars_zero(self):
        with trace.use(self.tracer()):
            self.assertEqual(memory.recall(FakeMemory("body", recall=False), "t1", 10), "")
            self.assertEqual(memory.recall(FakeMemory("body"), "t1", 0), "")
        found = self.events(trace.Kind.MEMORY_RECALL)
        self.assertEqual([event.attrs["chars"] for event in found], [0, 0])
        empty_sha = hashlib.sha256(b"").hexdigest()
        self.assertEqual([event.attrs["sha256"] for event in found], [empty_sha, empty_sha])

    def test_store_emits_stored_true_and_the_returned_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entry.md"
            conn = FakeMemory(path=path)
            with trace.use(self.tracer()):
                result = memory.store(
                    conn,
                    "t1",
                    {"HARNESS.md": "body"},
                    label="improve",
                    experience={"cue": "c", "lesson": "l", "evidence": "e"},
                )
        self.assertEqual(result, path)
        event = self.one(trace.Kind.MEMORY_STORE)
        self.assertEqual(
            dict(event.attrs),
            {
                "connector": "FakeMemory",
                "task_id": "t1",
                "label": "improve",
                "stored": True,
                "path": str(path),
            },
        )

    def test_store_is_stored_true_even_when_the_connector_returns_none(self):
        conn = FakeMemory(path=None)
        with trace.use(self.tracer()):
            result = memory.store(
                conn,
                "t1",
                {"HARNESS.md": "body"},
                experience={"cue": "c", "lesson": "l", "evidence": "e"},
            )
        self.assertIsNone(result)
        self.assertEqual(len(conn.added), 1)
        event = self.one(trace.Kind.MEMORY_STORE)
        self.assertIs(event.attrs["stored"], True)
        self.assertIsNone(event.attrs["path"])

    def test_incomplete_experience_emits_stored_false_and_adds_nothing(self):
        conn = FakeMemory()
        with trace.use(self.tracer()):
            self.assertIsNone(memory.store(conn, "t1", {"HARNESS.md": "body"}, experience={}))
        self.assertEqual(conn.added, [])
        event = self.one(trace.Kind.MEMORY_STORE)
        self.assertIs(event.attrs["stored"], False)
        self.assertIsNone(event.attrs["path"])

    def test_a_grader_verdict_stops_the_store_before_the_event(self):
        conn = FakeMemory()
        with trace.use(self.tracer()):
            with mock.patch.object(
                memory.context,
                "assert_tree_has_no_grader_verdicts",
                side_effect=ValueError("verdict"),
            ):
                with self.assertRaises(ValueError):
                    memory.store(
                        conn,
                        "t1",
                        {"HARNESS.md": "body"},
                        experience={"cue": "c", "lesson": "l", "evidence": "e"},
                    )
        self.assertEqual(conn.added, [])
        self.assertEqual(self.events(trace.Kind.MEMORY_STORE), [])


class ImproverTraceTest(TraceTestBase):
    def connector(self, choices, results, fences=None):
        conn = ImproverConnector(choices, results, fences)
        session = ModelSession(conn, "fake").for_task("t1")
        return conn, Proposer(CONNECTOR_SPEC, models=session)

    def run_connector(self, proposer):
        with tempfile.TemporaryDirectory() as tmp:
            return proposer._connector(Path(tmp), "mission", "", 30, "improve")

    def test_unavailable_moves_on_with_route_then_switch(self):
        conn, prop = self.connector([A, B], [GONE, OK])
        with trace.use(self.tracer()):
            text, _cost, _error, choice = self.run_connector(prop)
        self.assertEqual(text, "answer")
        self.assertEqual(choice, B)
        self.assertEqual(len(conn.sessions), 2)
        self.assertEqual(
            [event.kind for event in self.sink.events],
            [trace.Kind.MODEL_ROUTE, trace.Kind.MODEL_SWITCH],
        )
        route = self.events(trace.Kind.MODEL_ROUTE)[0]
        self.assertEqual(route.attrs["role"], "improver")
        self.assertEqual(
            [dict(candidate) for candidate in route.attrs["candidates"]],
            [choice_attrs(A), choice_attrs(B)],
        )
        switch = self.one(trace.Kind.MODEL_SWITCH)
        self.assertEqual(switch.attrs["role"], "improver")
        self.assertEqual(dict(switch.attrs["from"]), choice_attrs(A))
        self.assertEqual(dict(switch.attrs["to"]), choice_attrs(B))
        self.assertEqual(switch.attrs["reason"], "rate limited")

    def test_each_connector_call_emits_one_route(self):
        _conn, prop = self.connector([A], [OK, OK])
        with trace.use(self.tracer()):
            self.run_connector(prop)
            self.run_connector(prop)
        self.assertEqual(len(self.events(trace.Kind.MODEL_ROUTE)), 2)

    def test_the_switch_names_the_candidate_actually_tried_next(self):
        other = models.ModelChoice("opencode", "prov/x")
        conn, prop = self.connector(
            [A, other, B],
            [GONE, OK],
            fences={"claude": fence(), "opencode": fence(asks_human=True)},
        )
        with trace.use(self.tracer()):
            _text, _cost, _error, choice = self.run_connector(prop)
        self.assertEqual(choice, B)
        self.assertEqual(len(conn.sessions), 2)
        switch = self.one(trace.Kind.MODEL_SWITCH)
        self.assertEqual(dict(switch.attrs["to"]), choice_attrs(B))

    def test_a_switch_to_nothing_has_no_to(self):
        conn, prop = self.connector([A], [GONE])
        with trace.use(self.tracer()):
            text, _cost, _error, choice = self.run_connector(prop)
        self.assertEqual(text, "")
        self.assertIsNone(choice)
        switch = self.one(trace.Kind.MODEL_SWITCH)
        self.assertIsNone(switch.attrs["to"])

    def test_a_candidate_without_a_fence_emits_fence(self):
        other = models.ModelChoice("opencode", "prov/x")
        conn, prop = self.connector(
            [other, A],
            [OK],
            fences={"claude": fence(), "opencode": fence(asks_human=True)},
        )
        with trace.use(self.tracer()):
            _text, _cost, _error, choice = self.run_connector(prop)
        self.assertEqual(choice, A)
        self.assertEqual(len(conn.sessions), 1)
        event = self.one(trace.Kind.MODEL_FENCE)
        self.assertEqual(
            dict(event.attrs),
            {
                "role": "improver",
                "fenced": False,
                "runners": ["opencode"],
                "problems": ["no fence"],
                "model": "prov/x",
            },
        )


class ExecutorTraceTest(TraceTestBase):
    def selector(self, choices):
        return iw.ExecutorModels(ExecutorConnector(choices), substrate="claude", name="fake")

    def test_route_is_emitted_once_per_task_at_the_first_current(self):
        selector = self.selector([A, B])
        with trace.use(self.tracer()):
            self.assertEqual(selector.current("t1").model, "model-a")
            self.assertEqual(selector.current("t1").model, "model-a")
            self.assertEqual(selector.current("t2").model, "model-a")
        found = self.events(trace.Kind.MODEL_ROUTE)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].attrs["role"], "executor")
        self.assertEqual(
            [dict(candidate) for candidate in found[0].attrs["candidates"]],
            [choice_attrs(A), choice_attrs(B)],
        )
        self.assertEqual(found[0].attrs["skipped_other_runners"], 0)

    def test_route_counts_the_candidates_of_other_runners(self):
        selector = self.selector([A, models.ModelChoice("codex", "cx")])
        with trace.use(self.tracer()):
            selector.current("t1")
        route = self.one(trace.Kind.MODEL_ROUTE)
        self.assertEqual(route.attrs["skipped_other_runners"], 1)
        self.assertEqual(
            [candidate["model"] for candidate in route.attrs["candidates"]], ["model-a"]
        )

    def test_advance_emits_switch_and_the_end_has_no_to(self):
        selector = self.selector([A, B])
        with trace.use(self.tracer()):
            selector.current("t1")
            self.assertTrue(selector.advance("t1", A))
            self.assertFalse(selector.advance("t1", B))
        switches = self.events(trace.Kind.MODEL_SWITCH)
        self.assertEqual(len(switches), 2)
        self.assertEqual(dict(switches[0].attrs["from"]), choice_attrs(A))
        self.assertEqual(dict(switches[0].attrs["to"]), choice_attrs(B))
        self.assertEqual(switches[0].attrs["role"], "executor")
        self.assertEqual(dict(switches[1].attrs["from"]), choice_attrs(B))
        self.assertIsNone(switches[1].attrs["to"])

    def test_advance_that_does_not_move_emits_nothing(self):
        selector = self.selector([A, B])
        with trace.use(self.tracer()):
            selector.current("t1")
            self.assertTrue(selector.advance("t1", A))
            self.assertTrue(selector.advance("t1", A))  # another rollout already moved
        self.assertEqual(len(self.events(trace.Kind.MODEL_SWITCH)), 1)

    def test_no_candidates_emit_no_switch(self):
        selector = self.selector([])
        with trace.use(self.tracer()):
            with self.assertRaises(RuntimeError):
                selector.current("t1")
            self.assertFalse(selector.advance("t1", A))
        self.assertEqual(self.events(trace.Kind.MODEL_SWITCH), [])

    def test_two_threads_can_advance_without_stopping(self):
        selector = self.selector([A, B])
        with trace.use(self.tracer()):
            first = selector.current("t1")
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(trace.bind(selector.advance), "t1", first) for _ in range(2)]
                results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results, [True, True])
        switches = self.events(trace.Kind.MODEL_SWITCH)
        self.assertEqual(len(switches), 1)
        self.assertEqual(dict(switches[0].attrs["from"]), choice_attrs(A))
        self.assertEqual(dict(switches[0].attrs["to"]), choice_attrs(B))

    def test_a_bound_current_in_a_thread_keeps_the_outer_span_as_parent(self):
        selector = self.selector([A, B])
        with trace.use(self.tracer()):
            with trace.span(trace.Kind.TASK, task_id="t1") as span:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    choice = pool.submit(trace.bind(selector.current), "t1").result(timeout=10)
                outer = span.id
        self.assertEqual(choice, A)
        route = self.one(trace.Kind.MODEL_ROUTE)
        self.assertEqual(route.parent_id, outer)


class OutsideUseTest(TraceTestBase):
    def test_without_a_tracer_nothing_is_emitted_and_the_results_are_the_same(self):
        conn = FakeMemory("the lesson", path=None)
        experience = {"cue": "c", "lesson": "l", "evidence": "e"}
        self.assertEqual(memory.recall(conn, "t1", 100), "the lesson")
        self.assertIsNone(memory.store(conn, "t1", {"HARNESS.md": "body"}, experience=experience))
        self.assertIsNone(memory.store(conn, "t1", {"HARNESS.md": "body"}, experience={}))
        self.assertEqual(len(conn.added), 1)

        selector = iw.ExecutorModels(ExecutorConnector([A, B]), substrate="claude", name="fake")
        self.assertEqual(selector.current("t1"), A)
        self.assertTrue(selector.advance("t1", A))
        self.assertFalse(selector.advance("t1", B))

        improver = ImproverConnector([A, B], [GONE, OK])
        prop = Proposer(CONNECTOR_SPEC, models=ModelSession(improver, "fake").for_task("t1"))
        with tempfile.TemporaryDirectory() as tmp:
            text, _cost, _error, choice = prop._connector(Path(tmp), "m", "", 30, "improve")
        self.assertEqual(text, "answer")
        self.assertEqual(choice, B)

        self.assertEqual(self.sink.events, [])


if __name__ == "__main__":
    unittest.main()
