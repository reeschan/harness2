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

"""The file sink and the trace sink contract: append, read back, locks, defaults."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness2.core import trace
from harness2.core.trace_file import FILE_NAME, FileSink, read
from harness2.testing.trace_contract import (
    RecordingSink,
    TraceSinkContractTests,
    check_trace_sink,
)

REPO = Path(__file__).resolve().parents[1]

_TS = "2026-11-04T00:00:00+00:00"

_WRITER = """
import sys

from harness2.core.trace import Kind, TraceEvent
from harness2.core.trace_file import FileSink

root, tag, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
sink = FileSink(root)
for i in range(count):
    sink.emit(
        TraceEvent(
            run_id=tag,
            span_id="%s-%d" % (tag, i),
            parent_id=None,
            kind=Kind.COST,
            ts="2026-11-04T00:00:00+00:00",
            attrs={"i": i, "tag": tag, "pad": "x" * 200},
        )
    )
"""


def make_event(tag, index, **attrs):
    return trace.TraceEvent(
        run_id=tag,
        span_id=f"{tag}-{index}",
        parent_id=None,
        kind=trace.Kind.COST,
        ts=_TS,
        attrs={"i": index, **attrs},
    )


class FileSinkContractTest(TraceSinkContractTests, unittest.TestCase):
    def make_sink(self, root):
        return FileSink(root)


class RecordingSinkContractTest(TraceSinkContractTests, unittest.TestCase):
    def make_sink(self, root):
        return RecordingSink(root)


class FlushingSink:
    """A sink that keeps every event in `emit` and delivers them only in `close`."""

    def __init__(self, root=None):
        self.root = root
        self.events = []
        self._pending = []

    def emit(self, event):
        self._pending.append(event)

    def close(self):
        self.events.extend(self._pending)
        self._pending.clear()


class FlushingSinkContractTest(TraceSinkContractTests, unittest.TestCase):
    def make_sink(self, root):
        return FlushingSink(root)


class FileSinkCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def path(self):
        return self.root / FILE_NAME


class FileSinkTests(FileSinkCase):
    def test_file_name(self):
        self.assertEqual(FILE_NAME, "trace.jsonl")
        self.assertEqual(FileSink(self.root).path, self.path())

    def test_emit_appends_across_sink_instances(self):
        FileSink(self.root).emit(make_event("a", 0))
        FileSink(self.root).emit(make_event("a", 1))
        lines = self.path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)
        events = read(self.path())
        self.assertEqual([event.attrs["i"] for event in events], [0, 1])

    def test_emit_creates_the_root(self):
        root = self.root / "deeper" / "task"
        FileSink(root).emit(make_event("a", 0))
        self.assertTrue((root / FILE_NAME).is_file())

    def test_close_does_nothing(self):
        sink = FileSink(self.root)
        sink.close()
        sink.close()
        self.assertFalse(self.path().exists())

    def test_read_missing_file_is_empty(self):
        self.assertEqual(read(self.root / "missing.jsonl"), [])

    def test_read_skips_broken_lines(self):
        FileSink(self.root).emit(make_event("a", 0))
        with open(self.path(), "a", encoding="utf-8") as handle:
            handle.write("not json\n")
            handle.write("\n")
            handle.write('{"run_id": "only-a-run"}\n')
            handle.write(
                '{"run_id": "b", "span_id": "s", "kind": "cost", "ts": "t", "attrs": {"i": 1}}\n'
            )
        events = read(self.path())
        self.assertEqual([event.attrs.get("i") for event in events], [0, 1])

    def test_read_keeps_line_separator_characters_inside_events(self):
        sink = FileSink(self.root)
        values = ["a\u2028b", "a\u2029b", "a\u0085b"]
        for index, value in enumerate(values):
            sink.emit(make_event("a", index, x=value))
        events = read(self.path())
        self.assertEqual([event.attrs["x"] for event in events], values)
        self.assertEqual(len(self.path().read_text(encoding="ascii").split("\n")), 4)

    def test_emit_escapes_a_lone_surrogate(self):
        tracer = trace.Tracer(FileSink(self.root))
        tracer.event(trace.Kind.TREE_DIFF, files=[{"path": "a\udcff"}])
        tracer.close()
        self.assertEqual(tracer.warnings, [])
        self.path().read_bytes().decode("ascii")
        events = read(self.path())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].attrs["files"], [{"path": "a\udcff"}])

    def test_emit_repairs_a_torn_last_line(self):
        with open(self.path(), "w", encoding="utf-8") as handle:
            handle.write('{"run_id": "r", "spa')
        FileSink(self.root).emit(make_event("a", 0))
        events = read(self.path())
        self.assertEqual([event.attrs["i"] for event in events], [0])
        self.assertEqual(len(self.path().read_text(encoding="ascii").split("\n")), 3)

    def test_concurrent_appends_from_processes_keep_whole_lines(self):
        writers = ("a", "b", "c")
        per_writer = 150
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", _WRITER, str(self.root), tag, str(per_writer)],
                cwd=REPO,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for tag in writers
        ]
        for process in processes:
            _, error = process.communicate(timeout=180)
            self.assertEqual(process.returncode, 0, error)
        lines = self.path().read_text(encoding="utf-8").splitlines()
        expected = len(writers) * per_writer
        self.assertEqual(len(lines), expected)
        for line in lines:
            parsed = json.loads(line)
            self.assertIn(parsed["run_id"], writers)
        events = read(self.path())
        self.assertEqual(len(events), expected)
        self.assertEqual(len({event.span_id for event in events}), expected)

    def test_connector_for_defaults_to_the_file_sink(self):
        sink = trace.connector_for(self.root, env={})
        self.assertIsInstance(sink, FileSink)

    def test_open_for_writes_trace_jsonl_and_summary(self):
        tracer = trace.open_for(self.root, env={})
        tracer.event(trace.Kind.TASK, task_id="t1")
        tracer.close()
        summary = tracer.summary()
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["sink"], "file")
        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["warnings"], [])
        events = read(self.path())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].attrs["task_id"], "t1")
        self.assertEqual(events[0].run_id, summary["run_id"])

    def test_importing_trace_file_first_still_resolves_the_file_sink(self):
        code = (
            "import harness2.core.trace_file\n"
            "import harness2.core.trace\n"
            'print(harness2.core.trace.resolve_factory("file").__name__)\n'
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "FileSink")


class _ReorderingSink(RecordingSink):
    """A sink that holds a TASK start back until the next non-TASK event."""

    def __init__(self, root=None):
        super().__init__(root)
        self.held = None

    def emit(self, event):
        if event.kind == trace.Kind.TASK and event.attrs.get("phase") == "start":
            self.held = event
            return
        self.events.append(event)
        if self.held is not None:
            self.events.append(self.held)
            self.held = None


class ReorderingSinkTests(unittest.TestCase):
    def test_a_reordering_sink_fails_the_order_check(self):
        class Contract(TraceSinkContractTests, unittest.TestCase):
            def make_sink(self, root):
                return _ReorderingSink(root)

        case = Contract("test_trace_contract_events_are_read_in_order")
        try:
            with self.assertRaises(AssertionError):
                case.test_trace_contract_events_are_read_in_order()
        finally:
            case.doCleanups()


class CheckTraceSinkTests(unittest.TestCase):
    def test_accepts_a_recording_sink(self):
        check_trace_sink(RecordingSink())

    def test_rejects_an_object_without_emit_and_close(self):
        with self.assertRaises(AssertionError) as caught:
            check_trace_sink(object())
        self.assertIn("emit", str(caught.exception))
        self.assertIn("close", str(caught.exception))

    def test_rejects_members_that_are_not_callable(self):
        class Broken:
            emit = None
            close = None

        with self.assertRaises(AssertionError) as caught:
            check_trace_sink(Broken())
        self.assertIn("emit", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
