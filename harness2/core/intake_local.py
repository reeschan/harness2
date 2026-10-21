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

"""The local file connector: tasks come from a folder and results go back to a folder.

`LocalFileConnector` is the `local` task connector (the default of `intake.connector_for`).
It needs no outside service, so intake can run end to end in tests and on a workstation.
Everything lives under one root folder:

- `<root>/inbox/<id>/task.json` (optional): `{"title": ..., "body": ..., "meta": {...}}`.
  A `task_spec` entry in `meta` is used as is by the passthrough normalizer.
- `<root>/inbox/<id>/task.md` (optional): the body; the title is the first `# ` heading, or
  the id when there is none. When both files exist, task.json wins.
- `<root>/inbox/<id>/files/` (optional): the attachments.
- `<root>/outbox/<id>/`: a delivered task; a copy of the deliverable directory's contents,
  plus `summary.json`.
- `<root>/failed/<id>.json`: a failed task, `{"task_id": ..., "reason": ...}`.

A task id must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`. Inbox folders with other names are
skipped, and `deliver` and `fail` refuse such ids, so nothing is written outside the root. A
task with an outbox folder or a failed record is not polled again. A task whose task.json (or
task.md) cannot be read is recorded as failed by `poll` and skipped, so one broken folder
does not stop the others; a task.json or task.md that is a symbolic link (or not a regular
file) counts as unreadable, and a task.json that cannot be read never falls back to task.md.
Symbolic links are never followed or copied, neither from `files/` nor from a deliverable
directory.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping

from .intake import RawTask

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")  # used with fullmatch


def _valid_id(task_id: Any) -> bool:
    return isinstance(task_id, str) and _ID_RE.fullmatch(task_id) is not None


def _check_id(task_id: Any) -> str:
    if not _valid_id(task_id):
        raise ValueError(f"task id {task_id!r} does not match the pattern {_ID_PATTERN!r}")
    return task_id


def _regular_files(top: Path) -> list[str]:
    """The regular files under `top` as sorted POSIX paths relative to it; symbolic links
    (to files or directories) are neither listed nor followed.
    """
    out: list[str] = []
    if not top.is_dir() or top.is_symlink():
        return out
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
        for name in filenames:
            path = here / name
            if path.is_symlink() or not path.is_file():
                continue
            out.append(path.relative_to(top).as_posix())
    return sorted(out)


def _skip_links(directory: str, names: list[str]) -> list[str]:
    """A `shutil.copytree` ignore hook keeping only regular files and real directories."""
    skipped = []
    for name in names:
        path = os.path.join(directory, name)
        if os.path.islink(path) or not (os.path.isfile(path) or os.path.isdir(path)):
            skipped.append(name)
    return skipped


class _BadTask(ValueError):
    """A task folder whose task file cannot be read; `poll` records it as failed."""


def _read_task_file(path: Path) -> str:
    """The UTF-8 text of a task file (task.json or task.md) that must be a regular file.

    A symbolic link or anything other than a regular file, an unreadable file, or text that
    is not UTF-8 raises `_BadTask` with the file name in the reason. The file is opened
    without following links, so a link swapped in after the check is refused too.
    """
    name = path.name
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            raise _BadTask(f"{name}: is a symbolic link")
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise _BadTask(f"{name}: not a regular file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise IsADirectoryError(f"{name} is not a regular file")
            data = handle.read()
    except OSError as exc:
        raise _BadTask(f"{name}: cannot read: {type(exc).__name__}: {exc}") from exc
    try:
        return data.decode("utf-8")
    except ValueError as exc:  # UnicodeDecodeError
        raise _BadTask(f"{name}: not valid UTF-8: {exc}") from exc


def _title_from_markdown(text: str, default: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or default
    return default


class LocalFileConnector:
    """A task connector over a local folder; see the module docstring for the layout."""

    def __init__(self, root: Path | str, **_ignored: Any) -> None:
        if root is None:
            raise TypeError("LocalFileConnector needs a root folder")
        self.root = Path(root)

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def outbox(self) -> Path:
        return self.root / "outbox"

    @property
    def failed(self) -> Path:
        return self.root / "failed"

    def _done(self, task_id: str) -> bool:
        return (self.outbox / task_id).exists() or (self.failed / f"{task_id}.json").exists()

    def _read(self, task_id: str, folder: Path) -> RawTask:
        title, body, meta = task_id, "", {}
        json_path, md_path = folder / "task.json", folder / "task.md"
        if os.path.lexists(json_path):
            text = _read_task_file(json_path)
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise _BadTask(f"task.json: not valid JSON: {exc}") from exc
            if not isinstance(data, Mapping):
                raise _BadTask("task.json: must hold a JSON object")
            title = data.get("title") or task_id
            body = data.get("body") or ""
            meta = data.get("meta") or {}
            if not isinstance(title, str):
                raise _BadTask("task.json: 'title' must be a string")
            if not isinstance(body, str):
                raise _BadTask("task.json: 'body' must be a string")
            if not isinstance(meta, Mapping):
                raise _BadTask("task.json: 'meta' must be an object")
        elif os.path.lexists(md_path):
            body = _read_task_file(md_path)
            title = _title_from_markdown(body, task_id)
        files = tuple(_regular_files(folder / "files"))
        return RawTask(id=task_id, title=title, body=body, files=files, meta=dict(meta))

    def poll(self) -> Iterable[RawTask]:
        """The inbox tasks in name order that are neither delivered nor failed.

        A folder whose task.json or task.md cannot be used (a symbolic link or not a regular
        file, unreadable, not UTF-8, or a malformed task.json) is recorded with `fail` (the
        reason names the file and the problem) and skipped; polling goes on with the other
        folders.
        """
        if not self.inbox.is_dir():
            return []
        tasks: list[RawTask] = []
        for folder in sorted(self.inbox.iterdir(), key=lambda p: p.name):
            task_id = folder.name
            if not _valid_id(task_id) or folder.is_symlink() or not folder.is_dir():
                continue
            if self._done(task_id):
                continue
            try:
                tasks.append(self._read(task_id, folder))
            except _BadTask as exc:
                self.fail(task_id, str(exc))
        return tasks

    def fetch_files(self, raw: RawTask, dest: Path) -> None:
        """Copy the task's `files/` tree under `dest`, skipping symbolic links."""
        src = self.inbox / _check_id(raw.id) / "files"
        dest = Path(dest)
        for rel in _regular_files(src):
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, target, follow_symlinks=False)

    def deliver(self, task_id: str, deliverable_dir: Path, summary: Mapping) -> None:
        """Replace `outbox/<task_id>/` with a copy of `deliverable_dir` plus `summary.json`.

        Only regular files and directories are copied; symbolic links are skipped, not
        followed, so the outbox never points outside the deliverable directory.
        A task id outside the pattern or a missing deliverable directory raises ValueError.
        """
        _check_id(task_id)
        deliverable_dir = Path(deliverable_dir)
        if not deliverable_dir.is_dir():
            raise ValueError(f"deliverable directory {deliverable_dir} does not exist")
        text = json.dumps(dict(summary), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        self.outbox.mkdir(parents=True, exist_ok=True)
        # Build next to the final folder and rename, so a half-copied outbox never marks
        # the task as delivered. The leading dot keeps the staging name outside the pattern.
        staging = self.outbox / f".{task_id}.partial"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(deliverable_dir, staging, symlinks=True, ignore=_skip_links)
        (staging / "summary.json").write_text(text, encoding="utf-8")
        final = self.outbox / task_id
        if final.exists():
            shutil.rmtree(final)
        staging.rename(final)

    def fail(self, task_id: str, reason: str) -> None:
        """Write `failed/<task_id>.json`; a task id outside the pattern raises ValueError."""
        _check_id(task_id)
        self.failed.mkdir(parents=True, exist_ok=True)
        text = json.dumps(
            {"task_id": task_id, "reason": reason}, indent=2, sort_keys=True, ensure_ascii=False
        )
        tmp = self.failed / f".{task_id}.json.partial"
        tmp.write_text(text + "\n", encoding="utf-8")
        tmp.replace(self.failed / f"{task_id}.json")
