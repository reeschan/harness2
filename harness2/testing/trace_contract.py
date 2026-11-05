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

"""Contract checks for trace sinks, for sink authors to run from their own tests.

The trace sink contract is described in `harness2.core.trace`: a sink has `emit(event)` for one
`TraceEvent` and `close()` for the end of the run. A sink must not drop or reorder the events
the `Tracer` hands it, must accept emission from several threads, and must let `close` be called
more than once. The `Tracer` does the cleaning, so a sink only ever sees events whose attrs are
JSON-ready, with every string longer than `MAX_ATTR_CHARS` already replaced by its sha256. The
bodies therefore never reach a sink.

- `check_trace_sink(sink)` raises AssertionError when `sink` is not a `TraceSink`, or when
  `emit` or `close` is missing or not callable. The message names the missing or invalid members.
- `TraceSinkContractTests` is a unittest mixin that runs these checks, and a few more, against
  the sink your test builds: `check_trace_sink`; events emitted through a `Tracer` are read back
  in order, with one start and one end event per span; `close` can be called twice; four threads
  can emit at once and every event arrives with a unique span id; a 600-character attr reaches
  the sink as `{"sha256": <hex>, "chars": 600}`.
- `RecordingSink` keeps every event it receives in `events` (and counts `close` calls), so your
  own tests can inspect what a `Tracer` emitted without writing a file.

Example::

    import unittest
    from pathlib import Path

    from harness2.testing.trace_contract import TraceSinkContractTests
    from my_package.trace import MySink


    class MySinkContractTest(TraceSinkContractTests, unittest.TestCase):
        def make_sink(self, root: Path):
            return MySink(root=root)

The mixin does not inherit from `unittest.TestCase`, so unittest does not collect it on its
own; list `unittest.TestCase` after it in your test class. Each test calls `make_sink` with a
fresh temporary directory. The tests read the events back with `read_events(sink, root)`, which
returns `sink.events` when the sink keeps one and otherwise reads `root / "trace.jsonl"`; a sink
that stores events somewhere else (a database, a tracing backend) overrides `read_events`.
"""

from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from harness2.core import trace
from harness2.core.trace_file import FILE_NAME, read

_THREADS = 4
_PER_THREAD = 25


def check_trace_sink(sink: Any) -> None:
    """Assert that `sink` meets the trace sink contract.

    `emit` and `close` must be callable, and the object must satisfy the `TraceSink` Protocol.
    A missing or invalid member raises AssertionError whose message names it.
    """
    problems = [name for name in ("emit", "close") if not callable(getattr(sink, name, None))]
    if problems or not isinstance(sink, trace.TraceSink):
        detail = ", ".join(problems) or "the TraceSink Protocol check failed"
        raise AssertionError(
            f"{type(sink).__name__} is not a TraceSink; missing or invalid: {detail}"
        )


class RecordingSink:
    """A sink that keeps every event it receives in `events`, for use in your own tests."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else None
        self.events: list[trace.TraceEvent] = []
        self.closed = 0

    def emit(self, event: trace.TraceEvent) -> None:
        """Keep `event` in `events`."""
        self.events.append(event)

    def close(self) -> None:
        """Count the close."""
        self.closed += 1


class TraceSinkContractTests:
    """A unittest mixin that checks a sink against the trace sink contract.

    Subclass it together with `unittest.TestCase` and implement `make_sink(root)`. Each test
    passes a fresh, empty temporary directory as `root`. Override `read_events` when the sink
    does not keep `events` and does not write `root / "trace.jsonl"`.
    """

    def make_sink(self, root: Path) -> trace.TraceSink:
        """Build the sink under test for `root`."""
        raise NotImplementedError("subclasses of TraceSinkContractTests implement make_sink")

    def read_events(self, sink: trace.TraceSink, root: Path) -> list[trace.TraceEvent]:
        """The events `sink` received, in order.

        The default returns `sink.events` when the sink keeps it and otherwise reads
        `root / "trace.jsonl"`. A sink that stores events elsewhere overrides this.
        """
        events = getattr(sink, "events", None)
        if isinstance(events, list):
            return list(events)
        return read(root / FILE_NAME)

    # -- helpers -------------------------------------------------------------------------

    def _tmp(self) -> Path:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)  # type: ignore[attr-defined]
        return Path(holder.name)

    def _sink(self) -> tuple[trace.TraceSink, Path]:
        root = self._tmp() / "trace"
        root.mkdir()
        sink = self.make_sink(root)
        self.addCleanup(sink.close)  # type: ignore[attr-defined]
        return sink, root

    # -- tests ---------------------------------------------------------------------------

    def test_trace_contract_check_trace_sink(self) -> None:
        sink, _ = self._sink()
        check_trace_sink(sink)

    def test_trace_contract_events_are_read_in_order(self) -> None:
        sink, root = self._sink()
        tracer = trace.Tracer(sink, run_id="contract")
        self.addCleanup(tracer.close)  # type: ignore[attr-defined]
        tracer.event(trace.Kind.COST, seq=0)
        with tracer.span(trace.Kind.TASK):
            tracer.event(trace.Kind.MEMORY_RECALL, seq=1)
        tracer.event(trace.Kind.ERROR, seq=2)
        events = self.read_events(sink, root)
        if any(event.run_id != tracer.run_id for event in events):
            raise AssertionError("every event must carry the tracer's run_id")
        seqs = [event.attrs.get("seq") for event in events if "seq" in event.attrs]
        if seqs != [0, 1, 2]:
            raise AssertionError(f"events must read back in emit order [0, 1, 2], got {seqs!r}")
        task = [event for event in events if event.kind == trace.Kind.TASK]
        phases = [event.attrs.get("phase") for event in task]
        if phases != ["start", "end"]:
            raise AssertionError(f"a span must read back as start then end, got {phases!r}")
        if task[0].span_id != task[1].span_id:
            raise AssertionError("the start and end of a span must share a span_id")

    def test_trace_contract_close_can_be_called_twice(self) -> None:
        sink, _ = self._sink()
        sink.close()
        sink.close()

    def test_trace_contract_events_from_threads_all_arrive(self) -> None:
        sink, root = self._sink()
        tracer = trace.Tracer(sink)
        self.addCleanup(tracer.close)  # type: ignore[attr-defined]

        def work(index: int) -> None:
            for item in range(_PER_THREAD):
                tracer.event(trace.Kind.STAGE, worker=index, item=item)

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            list(pool.map(work, range(_THREADS)))
        events = self.read_events(sink, root)
        expected = _THREADS * _PER_THREAD
        if len(events) != expected:
            raise AssertionError(f"{expected} events were emitted, got {len(events)}")
        span_ids = {event.span_id for event in events}
        if len(span_ids) != expected:
            raise AssertionError(
                f"every event must have its own span_id, got {len(span_ids)} of {expected}"
            )

    def test_trace_contract_long_strings_reach_the_sink_hashed(self) -> None:
        sink, root = self._sink()
        tracer = trace.Tracer(sink)
        self.addCleanup(tracer.close)  # type: ignore[attr-defined]
        body = "x" * 600
        tracer.event(trace.Kind.ERROR, where="contract", message=body)
        events = self.read_events(sink, root)
        if not events:
            raise AssertionError("the emitted event did not reach the sink")
        message = events[0].attrs.get("message")
        expected = {"sha256": trace.digest(body), "chars": len(body)}
        if message != expected:
            raise AssertionError(
                f"a 600-character attr must reach the sink as {expected!r}, got {message!r}"
            )
