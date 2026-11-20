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

"""Trace sources: named readers that turn the records of some system into trace events.

A trace source has one member: `read(location)` reads the records at `location` (a string the
source interprets, usually a directory or a file) and returns the events and the warnings, as a
`SourceRead` or any `(events, warnings)` pair. Each event is a `SourcedEvent`: a
`trace.TraceEvent` plus the `Origin` it came from (the trace source's name, the file and the
1-based line), so a finding's evidence can be followed from the run graph node back to the line
of the original record. A line that cannot be read is a warning, not an error.

Trace sources are found by name the same way as the evaluators: a built-in registration
(`register`), then an entry point of the same name in the group `harness2.trace_sources`, then,
for a name containing `:`, a `module:Attr` import path. A factory is called as
`factory(**options)` and must return a `TraceSource`.

One trace source is built in: `trace` (`TraceFiles`) reads every `trace.jsonl` under the
location the way `harness2-evaluate --runs` does. A trace that cannot be read is a warning, and
a line that is not a complete event is skipped without one, as in `trace_file.read`. Its runs are
harness2 runs, so `graph.load_runs` reads its location as a root (with the `summary.json` next to
each trace) instead of passing its events through `check`.

The runs of every other trace source are external runs, and `check` keeps their events to the
rules: each run has one root `task` span whose attrs name the `system`; every other kind carries
the namespace `<system>.`; the built-in kinds (`trace.Kind`, and the `judge.` kinds) belong to
harness2 runs only, so an external run that uses them, or a `task` naming the system `harness2`,
breaks the rules. An event that breaks a rule is skipped with one warning.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Protocol, cast, runtime_checkable

from . import trace, trace_file

ENTRY_POINT_GROUP = "harness2.trace_sources"
TRACE = "trace"
HARNESS2 = "harness2"
SYSTEM_ATTR = "system"

BUILT_IN_KINDS: frozenset[str] = frozenset(
    value for name, value in vars(trace.Kind).items() if name.isupper() and isinstance(value, str)
)


@dataclass(frozen=True)
class Origin:
    """Where an event came from: the trace source's name, the file and the 1-based line."""

    name: str
    file: Path
    line: int

    def __post_init__(self) -> None:
        """Turn a string file into a `Path`."""
        if not isinstance(self.file, Path):
            object.__setattr__(self, "file", Path(self.file))

    def __str__(self) -> str:
        return f"{self.file}:{self.line} ({self.name})"


@dataclass(frozen=True)
class SourcedEvent:
    """One trace event and the place in the original record it was read from."""

    event: trace.TraceEvent
    origin: Origin


class SourceRead(NamedTuple):
    """What a trace source read: the events in record order and the warnings."""

    events: list[SourcedEvent]
    warnings: list[str]


@runtime_checkable
class TraceSource(Protocol):
    """A reader of one system's records: `read(location)` returns the events and the warnings."""

    def read(self, location: str) -> SourceRead:
        """The events at `location` and one warning per record that could not be read."""
        ...


Factory = Callable[..., TraceSource]

_REGISTRY: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    """Register a trace source factory under `name`; an existing entry of that name is replaced.

    The factory is called as `factory(**options)`.
    """
    _REGISTRY[name] = factory


def names() -> list[str]:
    """The built-in registered names plus the entry point names, sorted, without duplicates."""
    from . import evaluate

    return sorted(set(_REGISTRY) | {ep.name for ep in evaluate._entry_points(ENTRY_POINT_GROUP)})


def resolve_factory(name: str) -> Factory:
    """The factory for the trace source `name`.

    Looked up in this order: the built-in registrations (`register`), an entry point named
    `name` in the group `harness2.trace_sources`, and, for a name containing `:`, a
    `module:Attr` import path. A name found nowhere raises ValueError listing the registered
    and entry point names; a target that is not callable raises TypeError.
    """
    from . import evaluate

    return cast(
        Factory,
        evaluate._resolve_in(name, _REGISTRY, group=ENTRY_POINT_GROUP, what="trace source"),
    )


def trace_source_for(name: str, *, options: Mapping[str, Any] | None = None) -> TraceSource:
    """Build trace source `name` as `factory(**options)`.

    A factory result that is not a `TraceSource` raises TypeError.
    """
    if options is not None and not isinstance(options, Mapping):
        raise TypeError(
            f"trace source {name!r} options must be a mapping, not {type(options).__name__}"
        )
    factory = resolve_factory(name)
    source = factory(**dict(options or {}))
    if not isinstance(source, TraceSource) or not callable(source.read):
        raise TypeError(
            f"trace source {name!r} factory returned {type(source).__name__}, "
            "which is not a TraceSource"
        )
    return source


def read_from(source: TraceSource, location: str) -> SourceRead:
    """`source.read(location)` as a `SourceRead`.

    A result that is not an `(events, warnings)` pair of iterables raises TypeError. The events
    are not checked here; `check` does that.
    """
    result = source.read(location)
    try:
        events, warnings = result
        return SourceRead(list(events), [str(item) for item in warnings])
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"trace source {type(source).__name__} returned {type(result).__name__} for "
            f"{location!r}; expected (events, warnings)"
        ) from exc


def trace_paths(*locations: Path | str) -> list[Path]:
    """Every `trace.jsonl` file under the locations, sorted, each path once."""
    found: set[Path] = set()
    for location in locations:
        base = Path(location)
        found.update(path for path in base.rglob(trace_file.FILE_NAME) if path.is_file())
    return sorted(found)


class TraceFiles:
    """The built-in trace source `trace`: every `trace.jsonl` under the location."""

    def read(self, location: str) -> SourceRead:
        """The events of each trace under `location`, file by file, in line order.

        A trace that cannot be read adds a warning; a line that is not a complete event is
        skipped without one, as in `trace_file.read`.
        """
        events: list[SourcedEvent] = []
        warnings: list[str] = []
        for path in trace_paths(location):
            try:
                lines = _numbered_events(path)
            except OSError as exc:
                warnings.append(f"cannot read {path}: {exc}")
                continue
            for number, event in lines:
                events.append(SourcedEvent(event, Origin(TRACE, path, number)))
        return SourceRead(events, warnings)


def _numbered_events(path: Path) -> list[tuple[int, trace.TraceEvent]]:
    """The complete events of one `trace.jsonl` with their 1-based line numbers.

    The text is split on `"\\n"` only and a broken line is dropped, the same as
    `trace_file.read`.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    found: list[tuple[int, trace.TraceEvent]] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            found.append((number, trace.TraceEvent.from_dict(json.loads(line))))
        except (AttributeError, TypeError, ValueError, KeyError):
            continue
    return found


register(TRACE, TraceFiles)


def check(events: Iterable[Any]) -> tuple[list[SourcedEvent], list[str]]:
    """The events of external runs that keep the rules, and one warning per event skipped.

    Skipped: an item that is not a well-formed `SourcedEvent`; a `task` event whose span names
    no `system` (in any of its events) or names `harness2`; a built-in kind; an event of a run
    without a kept `task`; and a kind without the namespace `<system>.` of its run.
    """
    warnings: list[str] = []
    formed: list[SourcedEvent] = []
    for item in events:
        problem = _malformed(item)
        if problem is None:
            formed.append(item)
        else:
            warnings.append(problem)
    span_systems: dict[tuple[str, str], str] = {}
    for item in formed:
        event = item.event
        if event.kind != trace.Kind.TASK:
            continue
        system = event.attrs.get(SYSTEM_ATTR)
        if isinstance(system, str) and system:
            span_systems.setdefault((event.run_id, event.span_id), system)
    run_systems: dict[str, str] = {}
    for (run_id, _), system in span_systems.items():
        if system != HARNESS2:
            run_systems.setdefault(run_id, system)
    kept: list[SourcedEvent] = []
    for item in formed:
        problem = _rule_broken(item, span_systems, run_systems)
        if problem is None:
            kept.append(item)
        else:
            warnings.append(problem)
    return kept, warnings


def _malformed(item: Any) -> str | None:
    """Why `item` is not a usable `SourcedEvent`, or None when it is."""
    if not isinstance(item, SourcedEvent):
        return f"trace source gave {type(item).__name__}, not a SourcedEvent; skipped"
    origin = item.origin
    if not isinstance(origin, Origin):
        return f"trace source event has origin {type(origin).__name__}, not an Origin; skipped"
    event = item.event
    if not isinstance(event, trace.TraceEvent):
        return f"{origin}: {type(event).__name__} is not a TraceEvent; skipped"
    for name in ("run_id", "span_id", "kind"):
        value = getattr(event, name)
        if not isinstance(value, str) or not value:
            return f"{origin}: event {name} must be a non-empty string; skipped"
    if event.parent_id is not None and not isinstance(event.parent_id, str):
        return f"{origin}: event parent_id must be a string or null; skipped"
    if not isinstance(event.attrs, Mapping):
        return f"{origin}: event attrs must be a mapping; skipped"
    return None


def _rule_broken(
    item: SourcedEvent,
    span_systems: Mapping[tuple[str, str], str],
    run_systems: Mapping[str, str],
) -> str | None:
    """Which external run rule the event breaks, as a warning, or None when it keeps them."""
    event, origin = item.event, item.origin
    where = f"{origin}: {event.kind} {event.run_id}/{event.span_id}"
    if event.kind == trace.Kind.TASK:
        system = span_systems.get((event.run_id, event.span_id))
        if system is None:
            return f"{where} names no {SYSTEM_ATTR}; skipped"
        if system == HARNESS2:
            return f"{where} names the system {HARNESS2!r}, which only trace.jsonl may; skipped"
        return None
    if event.kind in BUILT_IN_KINDS or event.kind.startswith(trace.JUDGE_PREFIX):
        return f"{where} uses a built-in kind, which only harness2 runs may; skipped"
    system = run_systems.get(event.run_id)
    if system is None:
        return f"{where} belongs to no task that names a {SYSTEM_ATTR}; skipped"
    prefix = f"{system}."
    if not event.kind.startswith(prefix) or len(event.kind) == len(prefix):
        return f"{where} lacks the namespace {prefix!r} of its system; skipped"
    return None
