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

"""The default trace sink: one JSON object per line in `trace.jsonl`.

`FileSink(root)` appends every event to `root / FILE_NAME`. Each `emit` opens the file in
append mode, takes an exclusive `fcntl.flock`, writes one `json.dumps(event.to_dict())` line,
flushes and closes, so events appended by another process (a parallel task) never interleave
inside a line. `close` does nothing because nothing stays open.

`read(path)` reads a `trace.jsonl` back into events for the grading side and for the graph
building that comes later; a line that is not a complete event is skipped. The module does not
import `harness2.core.trace` at the top: `trace` imports this module at its own end to register
`FileSink`, so importing `harness2.core.trace_file` first must not start a cycle. The type is
imported only for type checkers, and `read` imports `TraceEvent` when it runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .trace import TraceEvent

FILE_NAME = "trace.jsonl"


class FileSink:
    """Append events to `root / "trace.jsonl"`, one JSON line per event."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.path = self.root / FILE_NAME

    def emit(self, event: TraceEvent) -> None:
        """Append `event` as one line, under an exclusive lock, and flush it to disk."""
        import fcntl

        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), sort_keys=True, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(line + "\n")
                handle.flush()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def close(self) -> None:
        """Do nothing: `emit` opens and closes the file each time."""


def read(path: Path | str) -> list[TraceEvent]:
    """The events in the `trace.jsonl` at `path`, in file order; broken lines are skipped.

    A missing file reads as an empty list. A line another process is still writing (or any line
    that is not a complete event) is dropped.
    """
    from .trace import TraceEvent

    text = Path(path).read_text(encoding="utf-8", errors="replace") if Path(path).is_file() else ""
    events: list[TraceEvent] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            events.append(TraceEvent.from_dict(json.loads(line)))
        except (AttributeError, TypeError, ValueError, KeyError):
            continue
    return events
