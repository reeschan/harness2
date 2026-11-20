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

"""Contract checks for trace sources, for trace source authors to run from their own tests.

The trace source contract is described in `harness2.core.trace_source`: `read(location)` returns
the events and the warnings, each event a `SourcedEvent` with the `Origin` it came from (the
trace source's name, the file and the 1-based line). The runs a trace source reads are external
runs, and their events keep the rules of `trace_source.check`: each run has one root `task` span
whose attrs name the `system` (not `harness2`); every other kind carries the namespace
`<system>.`; the built-in kinds (`trace.Kind` and the `judge.` kinds) belong to harness2 runs
only. The run graph keeps a broken event out with a warning; the contract makes it a failure.

- `check_trace_source(source, location, *, name=None)` raises AssertionError when `source` is
  not a `TraceSource`, when `read(location)` returns no `(events, warnings)` pair or no events,
  when an event breaks a rule of `trace_source.check`, when an origin names no trace source, no
  file or no line from 1 up (or, with `name`, another trace source), and when a run of the
  events has no single root `task` span or an event whose parent is missing. It returns what
  the trace source read, as a `SourceRead`.
- `contract_events(*, system="orchestrator", name="fixed", run_id="contract")` is a small
  external run that keeps the rules: a root `task` span naming `system`, a `<system>.step` span
  inside it and a `<system>.note` point event inside the step, each with its line in
  `records.jsonl`.
- `TraceSourceContractTests` is a unittest mixin. `setUp` makes a temporary directory, calls
  `make_location(root)` to write the records there and return the location, then calls
  `make_trace_source()`. Set `trace_source_name` to the name the origins must carry.
- `FixedTraceSource` returns the events and warnings it was given whatever the location, so a
  test can hand it events and get a trace source.

Example::

    import json
    import unittest

    from harness2.testing.trace_source_contract import TraceSourceContractTests

    from my_package.trace_source import MyTraceSource


    class MyTraceSourceContractTest(TraceSourceContractTests, unittest.TestCase):
        trace_source_name = "my_system"

        def make_location(self, root):
            records = root / "records.jsonl"
            records.write_text(json.dumps({"step": "start"}) + "\\n", encoding="utf-8")
            return str(root)

        def make_trace_source(self):
            return MyTraceSource()

The mixin does not inherit from `unittest.TestCase`, so unittest does not collect it on its
own; list `unittest.TestCase` after it in your test class. A subclass that overrides `setUp`
must call `super().setUp()`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

from harness2.core import graph, trace, trace_source
from harness2.core.trace_source import Origin, SourcedEvent, SourceRead, TraceSource

_RUN_ID = "contract"
_TS = "2026-01-01T00:00:00+00:00"
_RECORDS = "records.jsonl"


def contract_events(
    *, system: str = "orchestrator", name: str = "fixed", run_id: str = _RUN_ID
) -> list[SourcedEvent]:
    """A small external run of `system` that keeps the rules, read by trace source `name`.

    One root `task` span whose attrs name `system`, a `<system>.step` span inside it and a
    `<system>.note` point event inside the step, in record order on lines 1 to 5 of
    `records.jsonl`.
    """
    step = f"{system}.step"
    rows = (
        ("task", "t", None, {"phase": "start", trace_source.SYSTEM_ATTR: system}),
        (step, "s", "t", {"phase": "start"}),
        (f"{system}.note", "n", "s", {"text": "a step ran"}),
        (step, "s", "t", {"phase": "end", "status": "ok"}),
        ("task", "t", None, {"phase": "end", "status": "ok"}),
    )
    return [
        SourcedEvent(
            trace.TraceEvent(
                run_id=run_id,
                span_id=span_id,
                parent_id=parent_id,
                kind=kind,
                ts=_TS,
                attrs=attrs,
            ),
            Origin(name, Path(_RECORDS), line),
        )
        for line, (kind, span_id, parent_id, attrs) in enumerate(rows, start=1)
    ]


def check_trace_source(source: Any, location: str, *, name: str | None = None) -> SourceRead:
    """Assert that `source` meets the trace source contract at `location`; return what it read.

    `location` must hold at least one record, so that there are events to check. Every event
    must keep the rules of `trace_source.check` and carry a usable `Origin`; with `name`, every
    origin must name that trace source. Each run must have exactly one root, a `task` span, and
    every parent an event names must be in the same read.
    """
    read = getattr(source, "read", None)
    if not callable(read) or not isinstance(source, TraceSource):
        raise AssertionError(
            f"{type(source).__name__} is not a TraceSource; missing or invalid: read"
        )
    try:
        result = trace_source.read_from(source, location)
    except TypeError as exc:
        raise AssertionError(str(exc)) from exc
    if not result.events:
        raise AssertionError(
            f"read({location!r}) returned no events; the contract needs a location with records"
        )
    kept, problems = trace_source.check(result.events)
    if problems:
        raise AssertionError(
            "trace source events break the rules of external runs:\n" + "\n".join(problems)
        )
    for item in kept:
        _check_origin(item.origin, name)
    _check_runs(kept)
    return result


def _check_origin(origin: Origin, name: str | None) -> None:
    """Assert that `origin` names a trace source, a file and a 1-based line."""
    if not isinstance(origin.name, str) or not origin.name:
        raise AssertionError(f"origin {origin!r} must name the trace source")
    if name is not None and origin.name != name:
        raise AssertionError(
            f"origin {origin} names the trace source {origin.name!r}, not {name!r}"
        )
    if origin.file == Path():
        raise AssertionError(f"origin {origin} must name the file the event was read from")
    line = origin.line
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise AssertionError(f"origin {origin} must give a line number from 1 up, not {line!r}")


def _check_runs(events: Sequence[SourcedEvent]) -> None:
    """Assert that each run has one root, a `task` span, and no event with a missing parent."""
    built = graph.build(sources=[SourceRead(list(events), [])])
    if built.warnings:
        raise AssertionError(
            "trace source events do not form whole runs:\n" + "\n".join(built.warnings)
        )
    runs: dict[str, list[graph.Node]] = {}
    tasks: dict[str, list[graph.Node]] = {}
    for node in built.nodes.values():
        if node.parent is None:
            runs.setdefault(node.run_id, []).append(node)
        if node.kind == trace.Kind.TASK:
            tasks.setdefault(node.run_id, []).append(node)
    for run_id, roots in sorted(runs.items()):
        found = tasks.get(run_id, [])
        if len(roots) != 1 or roots[0].kind != trace.Kind.TASK or len(found) != 1:
            described = ", ".join(f"{node.kind} {node.span_id}" for node in _sorted(roots))
            spans = ", ".join(node.span_id for node in _sorted(found)) or "none"
            raise AssertionError(
                f"run {run_id} must have exactly one root, a task span; it has roots "
                f"[{described}] and task spans [{spans}]"
            )


def _sorted(nodes: Iterable[graph.Node]) -> list[graph.Node]:
    """`nodes` in span id order, for stable messages."""
    return sorted(nodes, key=lambda node: node.span_id)


class FixedTraceSource:
    """A trace source that returns the events and warnings it was given, for tests.

    `events` defaults to `contract_events()`. The location is ignored.
    """

    def __init__(
        self,
        events: Iterable[Any] | None = None,
        warnings: Iterable[str] = (),
    ) -> None:
        self._events = tuple(contract_events() if events is None else events)
        self._warnings = tuple(warnings)

    def read(self, location: str) -> SourceRead:
        """New lists of the events and the warnings this trace source was given."""
        return SourceRead(list(self._events), list(self._warnings))


class TraceSourceContractTests:
    """A unittest mixin that checks a trace source against the trace source contract.

    Subclass it together with `unittest.TestCase` and implement `make_location(root)` and
    `make_trace_source()`. `setUp` calls `make_location` with a new temporary directory first,
    so the records exist before the trace source is built. Set `trace_source_name` to the name
    every origin must carry; None checks only that each origin names one.
    """

    trace_source_name: str | None = None
    location: str

    def make_location(self, root: Path) -> str:
        """Write records under `root` and return the location the trace source reads."""
        raise NotImplementedError("subclasses of TraceSourceContractTests implement make_location")

    def make_trace_source(self) -> TraceSource:
        """Build the trace source under test; may read `self.location`."""
        raise NotImplementedError(
            "subclasses of TraceSourceContractTests implement make_trace_source"
        )

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)  # type: ignore[attr-defined]
        self.location = self.make_location(Path(holder.name))
        self.trace_source = self.make_trace_source()

    # -- tests ---------------------------------------------------------------------------

    def test_trace_source_contract_check_trace_source(self) -> None:
        result = check_trace_source(self.trace_source, self.location, name=self.trace_source_name)
        if not isinstance(result, SourceRead):
            raise AssertionError(f"check_trace_source must return a SourceRead, got {result!r}")

    def test_trace_source_contract_nodes_lead_back_to_the_records(self) -> None:
        result = trace_source.read_from(self.trace_source, self.location)
        built = graph.build(sources=[result])
        if not built.nodes:
            raise AssertionError("the trace source added no node to the run graph")
        for node in built.nodes.values():
            if not isinstance(node.source, Origin):
                raise AssertionError(
                    f"node {node.run_id}/{node.span_id} has source {node.source!r}, "
                    "not the Origin of its record"
                )
