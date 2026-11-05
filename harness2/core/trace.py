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

"""Trace sinks: the receiving end of the events a task run leaves behind.

One event is a `TraceEvent`: the run it belongs to, the span it belongs to, an optional
parent span, a kind, a UTC timestamp and JSON-ready attributes. A span is two events sharing
a `span_id`, one with `phase="start"` and one with `phase="end"`; a one-off occurrence is a
point event without a `phase`.

The rules keep the bodies out of the trace: prompts, artifacts and harness contents must not
be stored, and callers are responsible for not passing them. A file is a path and a sha256.
The `Tracer` replaces any string value longer than `MAX_ATTR_CHARS` with
`{"sha256": <hex>, "chars": <length>}`, including inside nested dicts and lists. The limit
applies to string values, not to mapping keys. It is a size-based safety net, not a check for
bodies: a body of `MAX_ATTR_CHARS` characters or fewer is kept, so the caller must not pass
one.

A sink is anything with `emit(event)` and `close()`. Sinks are found by name, like the memory
connectors and the model runners: `connector_name` picks the name (an explicit name, then the
`HARNESS2_TRACE_SINK` environment variable, then the default `file`), and `resolve_factory`
turns the name into a factory: a built-in registration (`register`), then an entry point of
the same name in the group `harness2.trace_sinks`, then, for a name containing `:`, a
`module:Attr` import path. A factory is called as `factory(root=Path, **options)` and must
return a `TraceSink`.

The current `Tracer` and the current span live in `contextvars`, so functions that do not
receive a tracer can still emit: `use(tracer)` installs one, `current()` reads it (a no-op
Tracer outside `use`) and the module-level `event` / `span` defer to it. `bind(fn)` carries
the caller's context into a thread pool. `open_for(task_dir)` builds the tracer for one task
run; tracing is on unless `HARNESS2_TRACE` is `"0"`. A failing sink never stops the run: the
failure is swallowed and kept once in `Tracer.warnings`, which `summary()` reports.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import importlib
import importlib.metadata
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Iterator,
    Mapping,
    Protocol,
    Sequence,
    TypeVar,
    cast,
    runtime_checkable,
)

ENABLE_VAR = "HARNESS2_TRACE"
ENV_VAR = "HARNESS2_TRACE_SINK"
DEFAULT = "file"
ENTRY_POINT_GROUP = "harness2.trace_sinks"

JUDGE_PREFIX = "judge."
MAX_ATTR_CHARS = 512

_MAX_WARNINGS = 20
_MAX_DEPTH = 32
_RESERVED_OPTIONS = ("root",)


class Kind:
    """The kinds of event a task run emits; never write the values by hand.

    Kinds starting with `JUDGE_PREFIX` are reserved for the grading side and are not emitted
    by the method side.
    """

    TASK = "task"
    ROLLOUT = "rollout"
    STAGE = "stage"
    TREE_DIFF = "tree.diff"
    SELECTION = "selection"
    REFLECTION = "reflection"
    MEMORY_RECALL = "memory.recall"
    MEMORY_STORE = "memory.store"
    MODEL_ROUTE = "model.route"
    MODEL_SWITCH = "model.switch"
    MODEL_FENCE = "model.fence"
    COST = "cost"
    ERROR = "error"


@dataclass(frozen=True)
class TraceEvent:
    """One line of a trace: who (run, span, parent), what (kind), when (ts) and attrs."""

    run_id: str
    span_id: str
    parent_id: str | None
    kind: str
    ts: str
    attrs: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """The six keys as a JSON-ready dict; `attrs` is copied."""
        return {
            "run_id": self.run_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "ts": self.ts,
            "attrs": dict(self.attrs),
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> TraceEvent:
        """The event in `d`; a missing `attrs` is `{}` and a non-mapping one raises TypeError."""
        attrs = d.get("attrs")
        if attrs is None:
            attrs = {}
        elif not isinstance(attrs, Mapping):
            raise TypeError(f"trace event attrs must be a mapping, not {type(attrs).__name__}")
        return cls(
            run_id=d["run_id"],
            span_id=d["span_id"],
            parent_id=d.get("parent_id"),
            kind=d["kind"],
            ts=d["ts"],
            attrs=attrs,
        )


@runtime_checkable
class TraceSink(Protocol):
    """Where events go: `emit` one event, `close` when the run is done."""

    def emit(self, event: TraceEvent) -> None: ...

    def close(self) -> None: ...


Factory = Callable[..., TraceSink]

_REGISTRY: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    """Register a sink factory under `name`; an existing entry of that name is replaced.

    The factory is called as `factory(root=Path, **options)`.
    """
    _REGISTRY[name] = factory


def _entry_points() -> list[Any]:
    """The installed entry points of `ENTRY_POINT_GROUP`."""
    return list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def names() -> list[str]:
    """The built-in registered names plus the entry point names, sorted, without duplicates."""
    return sorted(set(_REGISTRY) | {ep.name for ep in _entry_points()})


def connector_name(*, name: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """The sink name: `name` when given, else `ENV_VAR` from `env` (or `os.environ` when
    `env` is None), else `DEFAULT`. `resolve_factory` turns the name into a factory.
    """
    if name is not None:
        return name
    return (os.environ if env is None else env).get(ENV_VAR) or DEFAULT


def _import_factory(name: str) -> Factory:
    """Load the factory named by a `module:Attr` import path; Attr may be a dotted chain.

    A malformed path, a failed import and a missing attribute raise ValueError; an attribute
    that is not callable raises TypeError.
    """
    module_name, _, attr_path = name.partition(":")
    if name.count(":") != 1 or not module_name or not attr_path:
        raise ValueError(f"trace sink {name!r} is not a valid import path; expected 'module:Attr'")
    try:
        target = importlib.import_module(module_name)
    except Exception as exc:
        raise ValueError(
            f"trace sink {name!r}: cannot import module {module_name!r}: {exc}"
        ) from exc
    for attr in attr_path.split("."):
        try:
            target = getattr(target, attr)
        except AttributeError as exc:
            raise ValueError(
                f"trace sink {name!r}: {module_name!r} has no attribute {attr_path!r}"
            ) from exc
    if not callable(target):
        raise TypeError(f"trace sink {name!r} is {type(target).__name__}, which is not callable")
    return target


def _load_entry_point(name: str, ep: Any) -> Factory:
    """Load an entry point as a factory; a failed load raises ValueError, a target that is not
    callable raises TypeError."""
    try:
        target = ep.load()
    except Exception as exc:
        raise ValueError(
            f"trace sink {name!r}: cannot load entry point {ENTRY_POINT_GROUP}/{name}: {exc}"
        ) from exc
    if not callable(target):
        raise TypeError(
            f"trace sink {name!r} entry point is {type(target).__name__}, which is not callable"
        )
    return target


def resolve_factory(name: str) -> Factory:
    """The factory for the sink `name`.

    Looked up in this order: the built-in registrations (`register`), an entry point named
    `name` in the group `harness2.trace_sinks`, and, for a name containing `:`, a `module:Attr`
    import path (see `_import_factory`). An entry point's `load()` must return a factory, the
    same as a `module:Attr` target: a callable called as `factory(root=Path, **options)`. A
    name found nowhere raises ValueError listing the registered and entry point names.
    """
    factory = _REGISTRY.get(name)
    if factory is not None:
        return factory
    eps = _entry_points()
    for ep in eps:
        if ep.name == name:
            return _load_entry_point(name, ep)
    if ":" in name:
        return _import_factory(name)
    registered = ", ".join(sorted(_REGISTRY)) or "(none)"
    entry_names = ", ".join(sorted({ep.name for ep in eps})) or "(none)"
    raise ValueError(
        f"unknown trace sink {name!r}; registered: {registered}; "
        f"entry points ({ENTRY_POINT_GROUP}): {entry_names}"
    )


def connector_for(
    root: Path | str,
    *,
    name: str | None = None,
    env: Mapping[str, str] | None = None,
    options: Mapping[str, Any] | None = None,
) -> TraceSink:
    """Build the sink for one task run.

    The name comes from `connector_name` and the factory from `resolve_factory` (built-in
    registration, then entry point, then `module:Attr`); the factory is called as
    `factory(root=Path, **options)`. `options` carries the sink-specific settings and must not
    set `root`: harness2 supplies it, and a factory cannot override it. A name found nowhere
    raises ValueError; a factory result that is not a TraceSink raises TypeError.
    """
    name = connector_name(name=name, env=env)
    factory = resolve_factory(name)
    extra = dict(options or {})
    reserved = sorted(key for key in extra if key in _RESERVED_OPTIONS)
    if reserved:
        raise ValueError(
            f"trace sink {name!r} options must not set {reserved}; harness2 supplies root itself"
        )
    sink = factory(root=Path(root), **extra)
    if not isinstance(sink, TraceSink) or not (callable(sink.emit) and callable(sink.close)):
        raise TypeError(
            f"trace sink {name!r} factory returned {type(sink).__name__}, which is not a TraceSink"
        )
    return sink


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """Tracing is on unless `HARNESS2_TRACE` is `"0"`; any other value (or none) is on."""
    return (os.environ if env is None else env).get(ENABLE_VAR) != "0"


def digest(data: Any) -> str:
    """The sha256 hex of `data`; this never raises.

    Bytes are hashed as they are. A str is hashed as UTF-8 with the `surrogatepass` error
    handler, so a lone surrogate does not raise. Anything else is turned into a string first;
    when even `str()` raises, a fixed stand-in with the type name is hashed instead.
    """
    if isinstance(data, bytes):
        raw = data
    else:
        if not isinstance(data, str):
            try:
                data = str(data)
            except Exception:
                data = _unprintable(data)
        try:
            raw = data.encode("utf-8", "surrogatepass")
        except Exception:
            raw = _unprintable(data).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _unprintable(value: Any) -> str:
    """A fixed stand-in naming the type of `value`; used when `str(value)` raises."""
    try:
        name = type(value).__name__
    except Exception:
        name = "?"
    return f"<unprintable {name}>"


def _error_text(exc: BaseException) -> str:
    """`"Type: message"` for `exc`, cut to `MAX_ATTR_CHARS`.

    A message whose `__str__` raises becomes `<unprintable>`, so building the text for the
    end event never replaces the escaping exception with the failure of its own `__str__`.
    """
    try:
        name = type(exc).__name__
    except Exception:
        name = "?"
    try:
        message = str(exc)
    except Exception:
        message = "<unprintable>"
    return f"{name}: {message}"[:MAX_ATTR_CHARS]


def _clean_string(text: str) -> Any:
    if len(text) > MAX_ATTR_CHARS:
        return {"sha256": digest(text), "chars": len(text)}
    return text


def _clean(value: Any, depth: int = 0) -> Any:
    """Turn an attr value into something JSON can hold, hashing long strings."""
    if depth > _MAX_DEPTH:
        return _clean_string(str(value))
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _clean_string(value)
    if isinstance(value, Mapping):
        return {str(key): _clean(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(item, depth + 1) for item in value]
    return _clean_string(str(value))


def _new_span_id() -> str:
    return uuid.uuid4().hex[:16]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Span:
    """The open span handed to a `with` body: its `id` and `set(**attrs)` for the end event."""

    id: str
    _end: dict[str, Any] = field(default_factory=dict)

    def set(self, **attrs: Any) -> None:
        """Add attrs to the end event; a later value of the same key wins."""
        self._end.update(attrs)


class Tracer:
    """Emits one run's events to a sink; `Tracer(None)` does nothing.

    `event` emits a point event and returns its span id. `span` emits a start and an end event
    with the same span id, sets the end event's `status` and `dur_s`, records an escaping
    exception as `status="error"` with `error` and re-raises it. A sink that raises never
    stops the caller: the failure is swallowed and kept in `warnings`.
    """

    def __init__(
        self,
        sink: TraceSink | None,
        *,
        run_id: str | None = None,
        sink_name: str = "",
        clock: Callable[[], datetime] | None = None,
        warnings: Sequence[str] = (),
    ) -> None:
        self._sink = sink
        self.run_id = run_id or uuid.uuid4().hex
        self._sink_name = sink_name
        self._clock = clock or _utcnow
        self.warnings: list[str] = list(warnings)
        self._lock = threading.RLock()
        self._events = 0
        self._closed = False

    def event(self, kind: str, /, **attrs: Any) -> str:
        """Emit a point event and return its span id ("" when the tracer does nothing)."""
        if self._sink is None:
            return ""
        span_id = _new_span_id()
        self._emit(kind, span_id, _SPAN.get(), attrs)
        return span_id

    @contextlib.contextmanager
    def span(self, kind: str, /, **attrs: Any) -> Iterator[Span]:
        """Emit the start and end events of one span; an exception ends it as an error."""
        if self._sink is None:
            yield Span("")
            return
        span_id = _new_span_id()
        parent = _SPAN.get()
        started = time.monotonic()
        span = Span(span_id)
        token = _SPAN.set(span_id)
        try:
            self._emit(kind, span_id, parent, {**attrs, "phase": "start"})
            try:
                yield span
            except BaseException as exc:
                end = dict(span._end)
                end.update(
                    phase="end",
                    status="error",
                    dur_s=round(time.monotonic() - started, 3),
                    error=_error_text(exc),
                )
                self._emit(kind, span_id, parent, end)
                raise
            end = dict(span._end)
            end.update(phase="end", status="ok", dur_s=round(time.monotonic() - started, 3))
            self._emit(kind, span_id, parent, end)
        finally:
            _SPAN.reset(token)

    def close(self) -> None:
        """Call the sink's `close` once; a second call does nothing."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sink = self._sink
            failure = None
            if sink is not None:
                try:
                    sink.close()
                except Exception as exc:
                    failure = f"close failed: {_error_text(exc)}"
        if failure is not None:
            self._warn(failure)

    def summary(self) -> dict:
        """The dict for summary.json's `trace`: `{"enabled": False}` when nothing is recorded."""
        if self._sink is None and not self._sink_name and not self.warnings:
            return {"enabled": False}
        with self._lock:
            return {
                "enabled": True,
                "sink": self._sink_name,
                "run_id": self.run_id,
                "events": self._events,
                "warnings": list(self.warnings),
            }

    def _emit(self, kind: str, span_id: str, parent: str | None, attrs: Mapping[str, Any]) -> None:
        """Build the event, clean it and hand it to the sink; every failure stays in warnings."""
        if self._closed or self._sink is None:
            return
        try:
            event = TraceEvent(
                run_id=self.run_id,
                span_id=span_id,
                parent_id=parent,
                kind=kind,
                ts=self._clock().isoformat(),
                attrs=_clean(dict(attrs)),
            )
        except Exception as exc:
            self._warn(f"event failed: {_error_text(exc)}")
            return
        failure = None
        with self._lock:
            if self._closed or self._sink is None:
                return
            try:
                self._sink.emit(event)
                self._events += 1
            except Exception as exc:
                failure = f"emit failed: {_error_text(exc)}"
        if failure is not None:
            self._warn(failure)

    def _warn(self, text: str) -> None:
        """Keep a warning once, up to `_MAX_WARNINGS` of them."""
        with self._lock:
            if text in self.warnings or len(self.warnings) >= _MAX_WARNINGS:
                return
            self.warnings.append(text)


_TRACER: contextvars.ContextVar[Tracer | None] = contextvars.ContextVar(
    "harness2_trace_tracer", default=None
)
_SPAN: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "harness2_trace_span", default=None
)
_NOOP = Tracer(None)

F = TypeVar("F", bound=Callable[..., Any])


@contextlib.contextmanager
def use(tracer: Tracer) -> Iterator[Tracer]:
    """Make `tracer` the current one and drop any span carried in, until the block ends.

    It does not close the tracer: the owner of `open_for` does.
    """
    tracer_token = _TRACER.set(tracer)
    span_token = _SPAN.set(None)
    try:
        yield tracer
    finally:
        _SPAN.reset(span_token)
        _TRACER.reset(tracer_token)


def current() -> Tracer:
    """The tracer of the enclosing `use`, or a no-op Tracer outside one."""
    return _TRACER.get() or _NOOP


def event(kind: str, /, **attrs: Any) -> str:
    """Emit a point event on the current tracer and return its span id."""
    return current().event(kind, **attrs)


def span(kind: str, /, **attrs: Any) -> contextlib.AbstractContextManager[Span]:
    """Open a span on the current tracer."""
    return current().span(kind, **attrs)


def bind(fn: F) -> F:
    """Wrap `fn` so it runs with the caller's current tracer and span, also in a thread pool.

    Each call enters a fresh copy of the context taken when `bind` was called, so the same
    wrapped function can run on several threads at once.
    """
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return ctx.copy().run(fn, *args, **kwargs)

    return cast(F, wrapper)


def open_for(
    task_dir: Path | str,
    *,
    env: Mapping[str, str] | None = None,
    name: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> Tracer:
    """The tracer for one task run writing under `task_dir`.

    Tracing off gives `Tracer(None)`. A sink that cannot be built gives `Tracer(None)` with the
    failure in `warnings` as `sink failed: ...`, so the run goes on.
    """
    if not enabled(env):
        return Tracer(None)
    sink_name = connector_name(name=name, env=env)
    try:
        sink = connector_for(task_dir, name=name, env=env, options=options)
    except Exception as exc:
        return Tracer(None, sink_name=sink_name, warnings=[f"sink failed: {_error_text(exc)}"])
    return Tracer(sink, sink_name=sink_name)


# The default `file` sink lives in its own module: importing it here keeps `fcntl` out of this
# module's import and lets `harness2.core.trace_file` be imported first without a cycle.
from .trace_file import FileSink  # noqa: E402

register(DEFAULT, FileSink)
