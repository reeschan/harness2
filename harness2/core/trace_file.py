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
append mode, takes an exclusive `fcntl.flock`, writes one `json.dumps(..., sort_keys=True,
ensure_ascii=True)` line, flushes and closes, so events appended by another process (a parallel
task) never interleave inside a line. `ensure_ascii=True` escapes every character, so a value
that is not valid UTF-8 on its own (a lone surrogate in a path, for example) is written as an
escape and can be read back; the file itself stays ASCII. A write that was torn off without a
final newline gets its newline repaired first, so the next event never joins the broken line.
`close` does nothing because nothing stays open.

`read(path)` reads a `trace.jsonl` back into events for the grading side and for the graph
building that comes later; a line that is not a complete event is skipped. It splits the text
on `"\\n"` only, so the line-separator characters U+2028, U+2029 and U+0085 inside a value do not
split an event. The module does not import `harness2.core.trace` at the top: `trace` imports this
module at its own end to register `FileSink`, so importing `harness2.core.trace_file` first must
not start a cycle. The type is imported only for type checkers, and `read` imports `TraceEvent`
when it runs.
"""

from __future__ import annotations

import json
import os
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
        """Append `event` as one ASCII JSON line under an exclusive lock, repairing a torn line."""
        import fcntl

        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), sort_keys=True, ensure_ascii=True) + "\n"
        with open(self.path, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() > 0:
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        line = "\n" + line
                handle.write(line.encode("ascii"))
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
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            events.append(TraceEvent.from_dict(json.loads(line)))
        except (AttributeError, TypeError, ValueError, KeyError):
            continue
    return events
