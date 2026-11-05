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

"""The trace port: TraceEvent, sink registration, Tracer, attrs and propagation."""

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from harness2.core import schedule, trace

FIXED = datetime(2026, 11, 4, 5, 2, 51, 123456, tzinfo=timezone.utc)


class RecordingSink:
    """Keeps every event in a list and counts the closes."""

    def __init__(self, root=None):
        self.root = root
        self.events = []
        self.closed = 0

    def emit(self, event):
        self.events.append(event)

    def close(self):
        self.closed += 1

    def of(self, kind):
        return [event for event in self.events if event.kind == kind]


def make_sink(root, **options):
    """A factory in the `factory(root=Path, **options)` shape that keeps its options."""
    sink = RecordingSink(root)
    sink.options = options
    return sink


class Unprintable(Exception):
    """An exception whose message cannot be printed."""

    def __str__(self):
        raise ZeroDivisionError("no message")


class FakeEntryPoint:
    def __init__(self, name, target):
        self.name = name
        self._target = target
        self.loaded = 0

    def load(self):
        self.loaded += 1
        return self._target


class RegistryCase(unittest.TestCase):
    """Saves the registry and hides the installed entry points for each test."""

    def setUp(self):
        saved = dict(trace._REGISTRY)
        self.addCleanup(self.restore, saved)
        self.entry_points = []
        patcher = patch.object(trace, "_entry_points", lambda: list(self.entry_points))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def restore(saved):
        trace._REGISTRY.clear()
        trace._REGISTRY.update(saved)


class TraceEventTests(unittest.TestCase):
    def test_to_dict_and_from_dict_round_trip(self):
        event = trace.TraceEvent(
            run_id="run",
            span_id="span",
            parent_id="parent",
            kind=trace.Kind.TASK,
            ts=FIXED.isoformat(),
            attrs={"task_id": "t1", "files": [{"path": "a", "sha256": "00"}]},
        )
        data = event.to_dict()
        self.assertEqual(sorted(data), ["attrs", "kind", "parent_id", "run_id", "span_id", "ts"])
        self.assertEqual(trace.TraceEvent.from_dict(data), event)

    def test_to_dict_copies_attrs(self):
        attrs = {"a": 1}
        data = trace.TraceEvent("r", "s", None, "k", "ts", attrs).to_dict()
        data["attrs"]["a"] = 2
        self.assertEqual(attrs, {"a": 1})

    def test_from_dict_without_attrs_gives_empty_dict(self):
        event = trace.TraceEvent.from_dict(
            {"run_id": "r", "span_id": "s", "parent_id": None, "kind": "k", "ts": "ts"}
        )
        self.assertEqual(dict(event.attrs), {})

    def test_from_dict_without_required_key_raises(self):
        with self.assertRaises(KeyError):
            trace.TraceEvent.from_dict({"span_id": "s"})

    def test_from_dict_keeps_an_attrs_mapping_as_is(self):
        attrs = {"a": 1}
        event = trace.TraceEvent.from_dict(
            {
                "run_id": "r",
                "span_id": "s",
                "parent_id": None,
                "kind": "k",
                "ts": "ts",
                "attrs": attrs,
            }
        )
        self.assertIs(event.attrs, attrs)

    def test_from_dict_with_non_mapping_attrs_raises_type_error(self):
        for attrs in ([1, 2], "text", 3):
            with self.subTest(attrs=attrs):
                with self.assertRaises(TypeError):
                    trace.TraceEvent.from_dict(
                        {
                            "run_id": "r",
                            "span_id": "s",
                            "parent_id": None,
                            "kind": "k",
                            "ts": "ts",
                            "attrs": attrs,
                        }
                    )

    def test_kind_values(self):
        self.assertEqual(trace.Kind.TASK, "task")
        self.assertEqual(trace.Kind.TREE_DIFF, "tree.diff")
        self.assertEqual(trace.Kind.MEMORY_RECALL, "memory.recall")
        self.assertEqual(trace.Kind.MODEL_ROUTE, "model.route")
        self.assertEqual(trace.JUDGE_PREFIX, "judge.")
        self.assertEqual(trace.MAX_ATTR_CHARS, 512)


class RegistrationTests(RegistryCase):
    def test_register_then_resolve(self):
        trace.register("rec", make_sink)
        self.assertIs(trace.resolve_factory("rec"), make_sink)

    def test_register_replaces(self):
        trace.register("rec", make_sink)
        trace.register("rec", RecordingSink)
        self.assertIs(trace.resolve_factory("rec"), RecordingSink)

    def test_builtin_registration_comes_first(self):
        ep = FakeEntryPoint("rec", RecordingSink)
        self.entry_points.append(ep)
        trace.register("rec", make_sink)
        self.assertIs(trace.resolve_factory("rec"), make_sink)
        self.assertEqual(ep.loaded, 0)

    def test_entry_point_comes_before_import_path(self):
        path = "harness2.core.trace:TraceEvent"
        self.entry_points.append(FakeEntryPoint(path, make_sink))
        self.assertIs(trace.resolve_factory(path), make_sink)

    def test_entry_point_by_name_and_env(self):
        self.entry_points.append(FakeEntryPoint("ext", make_sink))
        self.assertIs(trace.resolve_factory("ext"), make_sink)
        self.assertEqual(trace.connector_name(env={trace.ENV_VAR: "ext"}), "ext")

    def test_import_path_still_works(self):
        self.assertIs(trace.resolve_factory("harness2.core.trace:TraceEvent"), trace.TraceEvent)

    def test_import_path_without_attribute_raises(self):
        with self.assertRaises(ValueError) as caught:
            trace.resolve_factory("harness2.core.trace:missing")
        self.assertIn("missing", str(caught.exception))

    def test_import_path_that_is_not_callable_raises_type_error(self):
        with self.assertRaises(TypeError) as caught:
            trace.resolve_factory("harness2.core.trace:MAX_ATTR_CHARS")
        self.assertIn("MAX_ATTR_CHARS", str(caught.exception))

    def test_malformed_import_path_raises(self):
        with self.assertRaises(ValueError):
            trace.resolve_factory("harness2.core.trace:")

    def test_unknown_name_lists_registered_and_entry_points(self):
        trace.register("rec", make_sink)
        self.entry_points.append(FakeEntryPoint("ext", make_sink))
        with self.assertRaises(ValueError) as caught:
            trace.resolve_factory("nope")
        message = str(caught.exception)
        self.assertIn("unknown trace sink 'nope'", message)
        self.assertIn("rec", message)
        self.assertIn("ext", message)
        self.assertIn(trace.ENTRY_POINT_GROUP, message)

    def test_broken_entry_point_load_raises_value_error(self):
        class Broken:
            name = "broken"

            def load(self):
                raise ImportError("no module")

        self.entry_points.append(Broken())
        with self.assertRaises(ValueError) as caught:
            trace.resolve_factory("broken")
        self.assertIn(trace.ENTRY_POINT_GROUP, str(caught.exception))

    def test_uncallable_entry_point_raises_type_error(self):
        self.entry_points.append(FakeEntryPoint("number", 42))
        with self.assertRaises(TypeError):
            trace.resolve_factory("number")

    def test_names_are_sorted_and_unique(self):
        trace.register("b", make_sink)
        self.entry_points.extend([FakeEntryPoint("a", make_sink), FakeEntryPoint("b", make_sink)])
        names = trace.names()
        self.assertEqual(names, sorted({"a", "b", trace.DEFAULT}))

    def test_group_name(self):
        self.assertEqual(trace.ENTRY_POINT_GROUP, "harness2.trace_sinks")


class EntryPointLookupTests(unittest.TestCase):
    def test_entry_points_asks_for_the_group(self):
        calls = []

        def entry_points(**kwargs):
            calls.append(kwargs)
            return []

        with patch("importlib.metadata.entry_points", entry_points):
            trace._entry_points()
        self.assertEqual(calls, [{"group": trace.ENTRY_POINT_GROUP}])


class ConnectorTests(RegistryCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_builds_with_root_and_options(self):
        trace.register("rec", make_sink)
        sink = trace.connector_for(self.root, name="rec", env={}, options={"depth": 2})
        self.assertIsInstance(sink, trace.TraceSink)
        self.assertEqual(sink.root, self.root)
        self.assertEqual(sink.options, {"depth": 2})

    def test_name_comes_from_env(self):
        trace.register("rec", make_sink)
        sink = trace.connector_for(self.root, env={trace.ENV_VAR: "rec"})
        self.assertIsInstance(sink, RecordingSink)

    def test_root_option_raises(self):
        trace.register("rec", make_sink)
        with self.assertRaises(ValueError) as caught:
            trace.connector_for(self.root, name="rec", env={}, options={"root": "/elsewhere"})
        self.assertIn("root", str(caught.exception))

    def test_result_that_is_not_a_sink_raises_type_error(self):
        trace.register("bad", lambda root, **options: object())
        with self.assertRaises(TypeError) as caught:
            trace.connector_for(self.root, name="bad", env={})
        self.assertIn("bad", str(caught.exception))

    def test_result_whose_emit_or_close_is_not_callable_raises_type_error(self):
        class BadEmit:
            emit = 1

            def close(self):
                pass

        class BadClose:
            def emit(self, event):
                pass

            close = 1

        for factory in (
            lambda root, **options: BadEmit(),
            lambda root, **options: BadClose(),
        ):
            with self.subTest(factory=factory):
                trace.register("half", factory)
                with self.assertRaises(TypeError) as caught:
                    trace.connector_for(self.root, name="half", env={})
                self.assertIn("which is not a TraceSink", str(caught.exception))

    def test_unknown_name_raises_value_error(self):
        with self.assertRaises(ValueError):
            trace.connector_for(self.root, name="nope", env={})


class EnabledTests(unittest.TestCase):
    def test_on_by_default(self):
        self.assertTrue(trace.enabled({}))

    def test_zero_turns_it_off(self):
        self.assertFalse(trace.enabled({trace.ENABLE_VAR: "0"}))

    def test_other_values_stay_on(self):
        for value in ("1", "false", "", "off"):
            self.assertTrue(trace.enabled({trace.ENABLE_VAR: value}))

    def test_reads_os_environ_when_env_is_none(self):
        with patch.dict(os.environ, {trace.ENABLE_VAR: "0"}):
            self.assertFalse(trace.enabled())
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(trace.enabled())


class TracerTests(unittest.TestCase):
    def make(self, sink=None):
        return trace.Tracer(sink or RecordingSink(), sink_name="rec", clock=lambda: FIXED)

    def test_span_emits_start_and_end(self):
        sink = RecordingSink()
        tracer = self.make(sink)
        with tracer.span(trace.Kind.STAGE, label="s1") as span:
            span.set(usd=0.5)
        start, end = sink.events
        self.assertEqual(start.span_id, end.span_id)
        self.assertEqual(start.kind, "stage")
        self.assertEqual(start.attrs["phase"], "start")
        self.assertEqual(start.attrs["label"], "s1")
        self.assertEqual(end.attrs["phase"], "end")
        self.assertEqual(end.attrs["status"], "ok")
        self.assertEqual(end.attrs["usd"], 0.5)
        self.assertNotIn("label", end.attrs)
        self.assertGreaterEqual(end.attrs["dur_s"], 0)
        self.assertIsInstance(end.attrs["dur_s"], float)
        self.assertIsNone(start.parent_id)
        self.assertEqual(start.ts, FIXED.isoformat())

    def test_nested_spans_link_parent(self):
        sink = RecordingSink()
        tracer = self.make(sink)
        with tracer.span(trace.Kind.TASK) as outer:
            with tracer.span(trace.Kind.ROLLOUT) as inner:
                pass
        rollout_start = sink.of("rollout")[0]
        self.assertEqual(rollout_start.parent_id, outer.id)
        self.assertNotEqual(inner.id, outer.id)

    def test_point_event_is_its_own_span(self):
        sink = RecordingSink()
        tracer = self.make(sink)
        span_id = tracer.event(trace.Kind.TREE_DIFF, files=[])
        event = sink.events[0]
        self.assertEqual(event.span_id, span_id)
        self.assertIsNone(event.parent_id)
        self.assertNotIn("phase", event.attrs)

    def test_point_event_under_a_span_keeps_the_parent(self):
        sink = RecordingSink()
        tracer = self.make(sink)
        with tracer.span(trace.Kind.TASK) as outer:
            tracer.event(trace.Kind.COST, usd=1)
        cost = sink.of("cost")[0]
        self.assertEqual(cost.parent_id, outer.id)

    def test_exception_ends_the_span_as_error_and_is_reraised(self):
        sink = RecordingSink()
        tracer = self.make(sink)
        with self.assertRaises(ValueError):
            with tracer.span(trace.Kind.STAGE):
                raise ValueError("bad stage")
        end = sink.of("stage")[1]
        self.assertEqual(end.attrs["phase"], "end")
        self.assertEqual(end.attrs["status"], "error")
        self.assertEqual(end.attrs["error"], "ValueError: bad stage")

    def test_exception_whose_str_raises_is_reraised_as_itself(self):
        sink = RecordingSink()
        tracer = self.make(sink)
        with self.assertRaises(Unprintable):
            with tracer.span(trace.Kind.STAGE):
                raise Unprintable()
        end = sink.of("stage")[1]
        self.assertEqual(end.attrs["status"], "error")
        self.assertEqual(end.attrs["error"], "Unprintable: <unprintable>")

    def test_long_error_text_is_cut_not_hashed(self):
        sink = RecordingSink()
        tracer = self.make(sink)
        with self.assertRaises(RuntimeError):
            with tracer.span(trace.Kind.STAGE):
                raise RuntimeError("x" * 900)
        end = sink.of("stage")[1]
        self.assertIsInstance(end.attrs["error"], str)
        self.assertEqual(len(end.attrs["error"]), trace.MAX_ATTR_CHARS)
        self.assertTrue(end.attrs["error"].startswith("RuntimeError: "))

    def test_spans_in_sequence_do_not_nest(self):
        sink = RecordingSink()
        tracer = self.make(sink)
        with tracer.span(trace.Kind.STAGE):
            pass
        with tracer.span(trace.Kind.STAGE):
            pass
        second_start = sink.of("stage")[2]
        self.assertIsNone(second_start.parent_id)

    def test_close_calls_the_sink_once(self):
        sink = RecordingSink()
        tracer = self.make(sink)
        tracer.close()
        tracer.close()
        self.assertEqual(sink.closed, 1)

    def test_nothing_is_emitted_after_close(self):
        sink = RecordingSink()
        tracer = self.make(sink)
        tracer.close()
        tracer.event(trace.Kind.ERROR, where="x")
        self.assertEqual(sink.events, [])
        self.assertEqual(tracer.summary()["events"], 0)

    def test_summary_counts_delivered_events(self):
        sink = RecordingSink()
        tracer = self.make(sink)
        tracer.event(trace.Kind.ERROR, where="x")
        with tracer.span(trace.Kind.TASK):
            pass
        summary = tracer.summary()
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["sink"], "rec")
        self.assertEqual(summary["run_id"], tracer.run_id)
        self.assertEqual(summary["events"], 3)
        self.assertEqual(summary["warnings"], [])

    def test_disabled_tracer_does_nothing(self):
        tracer = trace.Tracer(None)
        self.assertEqual(tracer.event(trace.Kind.ERROR), "")
        with tracer.span(trace.Kind.TASK) as span:
            self.assertEqual(span.id, "")
        self.assertEqual(tracer.summary(), {"enabled": False})


class AttrsTests(unittest.TestCase):
    def make(self):
        sink = RecordingSink()
        return sink, trace.Tracer(sink, clock=lambda: FIXED)

    def test_long_string_is_hashed(self):
        sink, tracer = self.make()
        body = "x" * 513
        tracer.event(trace.Kind.ERROR, message=body)
        attrs = sink.events[0].attrs
        self.assertEqual(attrs["message"], {"sha256": trace.digest(body), "chars": 513})

    def test_exactly_the_limit_is_kept(self):
        sink, tracer = self.make()
        body = "x" * trace.MAX_ATTR_CHARS
        tracer.event(trace.Kind.ERROR, message=body)
        self.assertEqual(sink.events[0].attrs["message"], body)

    def test_long_string_in_nested_dict_and_list_is_hashed(self):
        sink, tracer = self.make()
        body = "y" * 600
        tracer.event(trace.Kind.TREE_DIFF, files=[{"path": body}], blob={"text": body})
        attrs = sink.events[0].attrs
        hashed = {"sha256": trace.digest(body), "chars": 600}
        self.assertEqual(attrs["files"][0]["path"], hashed)
        self.assertEqual(attrs["blob"]["text"], hashed)

    def test_non_string_keys_become_strings_and_dumps_sorts(self):
        sink, tracer = self.make()
        tracer.event(trace.Kind.COST, counts={1: "a", "b": 2})
        attrs = sink.events[0].attrs
        self.assertEqual(attrs["counts"], {"1": "a", "b": 2})
        json.dumps(sink.events[0].to_dict(), sort_keys=True)

    def test_other_values_are_stringified_or_kept(self):
        sink, tracer = self.make()
        tracer.event(trace.Kind.ERROR, path=Path("/tmp/x"), flag=True, n=3, nan=float("nan"))
        attrs = sink.events[0].attrs
        self.assertEqual(attrs["path"], "/tmp/x")
        self.assertIs(attrs["flag"], True)
        self.assertEqual(attrs["n"], 3)
        self.assertEqual(attrs["nan"], "nan")

    def test_digest_matches_the_scheduler(self):
        text = "some tree text"
        self.assertEqual(trace.digest(text), schedule._sha256(text))
        self.assertEqual(trace.digest(text.encode("utf-8")), schedule._sha256(text))

    def test_deep_nesting_falls_back_to_a_string(self):
        sink, tracer = self.make()
        value = "deep"
        for _ in range(trace._MAX_DEPTH + 5):
            value = {"n": value}
        tracer.event(trace.Kind.ERROR, value=value)
        self.assertTrue(sink.events)


class DigestTests(unittest.TestCase):
    def test_plain_str_and_bytes_are_hash_the_same_as_before(self):
        text = "some tree text"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual(trace.digest(text), expected)
        self.assertEqual(trace.digest(text.encode("utf-8")), expected)

    def test_other_values_do_not_raise(self):
        for value in (["x"], None, {"a": 1}, 3):
            with self.subTest(value=value):
                self.assertRegex(trace.digest(value), r"\A[0-9a-f]{64}\Z")

    def test_lone_surrogate_does_not_raise(self):
        expected = hashlib.sha256("\ud800".encode("utf-8", "surrogatepass")).hexdigest()
        self.assertEqual(trace.digest("\ud800"), expected)
        self.assertRegex(trace.digest("\ud800" * 600), r"\A[0-9a-f]{64}\Z")

    def test_value_whose_str_raises_does_not_raise(self):
        class Bad:
            def __str__(self):
                raise ZeroDivisionError("no text")

        self.assertRegex(trace.digest(Bad()), r"\A[0-9a-f]{64}\Z")


class FailureTests(unittest.TestCase):
    def test_emit_failure_does_not_stop_the_run_and_warns_once(self):
        class BrokenSink:
            def __init__(self):
                self.calls = 0

            def emit(self, event):
                self.calls += 1
                raise RuntimeError("boom")

            def close(self):
                raise RuntimeError("bye")

        sink = BrokenSink()
        tracer = trace.Tracer(sink, sink_name="broken", clock=lambda: FIXED)
        tracer.event(trace.Kind.ERROR, where="one")
        tracer.event(trace.Kind.ERROR, where="two")
        self.assertEqual(sink.calls, 2)
        self.assertEqual(tracer.warnings, ["emit failed: RuntimeError: boom"])
        summary = tracer.summary()
        self.assertEqual(summary["events"], 0)
        self.assertEqual(summary["warnings"], ["emit failed: RuntimeError: boom"])
        tracer.close()
        self.assertIn("close failed: RuntimeError: bye", tracer.warnings)

    def test_sink_can_call_the_tracer_from_emit(self):
        class ReentrantSink:
            def __init__(self):
                self.tracer = None
                self.summaries = 0

            def emit(self, event):
                if self.tracer is not None:
                    self.summaries += 1
                    self.tracer.summary()

            def close(self):
                pass

        sink = ReentrantSink()
        tracer = trace.Tracer(sink, clock=lambda: FIXED)
        sink.tracer = tracer
        worker = threading.Thread(target=tracer.event, args=(trace.Kind.ERROR,), daemon=True)
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(sink.summaries, 1)

    def test_many_different_failures_stop_at_twenty(self):
        class NoisySink:
            def __init__(self):
                self.n = 0

            def emit(self, event):
                self.n += 1
                raise RuntimeError(f"boom {self.n}")

            def close(self):
                pass

        tracer = trace.Tracer(NoisySink(), clock=lambda: FIXED)
        for _ in range(30):
            tracer.event(trace.Kind.ERROR)
        self.assertEqual(len(tracer.warnings), 20)


class ContextTests(RegistryCase):
    def test_current_outside_use_is_a_noop_tracer(self):
        tracer = trace.current()
        self.assertEqual(tracer.event(trace.Kind.ERROR), "")
        with trace.span(trace.Kind.TASK) as span:
            self.assertEqual(span.id, "")
        self.assertEqual(tracer.summary(), {"enabled": False})

    def test_use_sets_and_restores_the_current_tracer(self):
        sink = RecordingSink()
        tracer = trace.Tracer(sink, clock=lambda: FIXED)
        self.assertIs(trace.current(), trace._NOOP)
        with trace.use(tracer) as entered:
            self.assertIs(entered, tracer)
            self.assertIs(trace.current(), tracer)
            trace.event(trace.Kind.ERROR, where="inside")
        self.assertIs(trace.current(), trace._NOOP)
        self.assertEqual(len(sink.events), 1)

    def test_use_drops_the_span_of_the_outer_tracer(self):
        outer_sink = RecordingSink()
        inner_sink = RecordingSink()
        outer = trace.Tracer(outer_sink, clock=lambda: FIXED)
        inner = trace.Tracer(inner_sink, clock=lambda: FIXED)
        with trace.use(outer):
            with trace.span(trace.Kind.TASK):
                with trace.use(inner):
                    trace.event(trace.Kind.ERROR, where="inner")
                trace.event(trace.Kind.COST, usd=0)
        self.assertIsNone(inner_sink.events[0].parent_id)
        cost = outer_sink.of("cost")[0]
        self.assertEqual(cost.parent_id, outer_sink.of("task")[0].span_id)

    def test_bind_carries_the_parent_span_into_a_thread_pool(self):
        sink = RecordingSink()
        tracer = trace.Tracer(sink, clock=lambda: FIXED)

        def work(index):
            time.sleep(0.02)
            return trace.event("inner", index=index)

        with trace.use(tracer):
            with tracer.span(trace.Kind.TASK) as outer:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    ids = list(pool.map(trace.bind(work), range(8)))

        inner = [event for event in sink.events if event.kind == "inner"]
        self.assertEqual(len(inner), 8)
        self.assertEqual(sorted(event.span_id for event in inner), sorted(ids))
        self.assertTrue(all(event.parent_id == outer.id for event in inner))

    def test_bind_keeps_the_signature_and_name(self):
        def work(a, b=2):
            return a + b

        wrapped = trace.bind(work)
        self.assertEqual(wrapped.__name__, "work")
        self.assertEqual(wrapped(1), 3)
        self.assertEqual(wrapped(1, b=5), 6)


class OpenForTests(RegistryCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_disabled_summary(self):
        tracer = trace.open_for(self.root, env={trace.ENABLE_VAR: "0"})
        self.assertEqual(tracer.summary(), {"enabled": False})

    def test_enabled_builds_the_named_sink(self):
        trace.register("rec", make_sink)
        tracer = trace.open_for(self.root, env={}, name="rec")
        self.assertEqual(tracer.summary()["sink"], "rec")
        tracer.event(trace.Kind.ERROR, where="x")
        self.assertEqual(tracer.summary()["events"], 1)

    def test_missing_default_sink_warns_and_keeps_running(self):
        trace._REGISTRY.pop(trace.DEFAULT, None)
        tracer = trace.open_for(self.root, env={})
        summary = tracer.summary()
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["sink"], "file")
        self.assertEqual(summary["events"], 0)
        self.assertEqual(len(summary["warnings"]), 1)
        self.assertTrue(summary["warnings"][0].startswith("sink failed: ValueError"))
        tracer.event(trace.Kind.ERROR, where="x")
        self.assertEqual(tracer.summary()["events"], 0)


if __name__ == "__main__":
    unittest.main()
