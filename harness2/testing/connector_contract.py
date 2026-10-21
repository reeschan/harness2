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

"""Contract checks for task connectors, for connector authors to run from their own tests.

The task connector contract is described in `harness2.core.intake`: the four required methods
of `TaskConnector` (poll / fetch_files / deliver / fail), the optional capabilities read by
`capabilities(connector)` (workspace_policy / scrub_env / judge / report / close), and the
shape of a Judge's result (None, or a dict with `n_passed`, `n_criteria` and optionally
`all_pass`, which is what the harness's scoring record reads).

- `check_connector(connector)` raises AssertionError when `connector` is not a TaskConnector
  or one of its optional capabilities has the wrong type.
- `check_judge_result(result)` raises AssertionError when a Judge's result has the wrong shape.
- `check_fetched_files(dest)` raises AssertionError when `fetch_files` left something wrong at
  `dest`. Leaving `dest` missing is fine (a task without attachments need not create it); when
  it exists it must be a directory with no symbolic links and nothing outside it.
- `ConnectorContractTests` is a unittest mixin that runs these checks, and a few more, against
  the connector your test builds: `check_connector`; `poll()` yields only RawTask; for each
  polled task `fetch_files` does not raise and passes `check_fetched_files`; `judge` (if any)
  passes `check_judge_result`; `report` (if any) returns str or None; `close` does not raise.

Example::

    import unittest
    from pathlib import Path

    from harness2.testing.connector_contract import ConnectorContractTests
    from my_package.connector import MyConnector


    class MyConnectorContractTest(ConnectorContractTests, unittest.TestCase):
        def make_connector(self, tmp: Path):
            # Put at least one task where the connector will find it, so that
            # poll / fetch_files / judge are exercised on a real task. Cover both a
            # task with attachments and one without (e.g. in two test classes).
            return MyConnector(root=tmp)

        # Optional: give the judge a run directory that looks like a finished run.
        def make_run_dir(self, tmp: Path, task_id: str) -> Path:
            run_dir = super().make_run_dir(tmp, task_id)
            (run_dir / "output").mkdir()
            return run_dir

The mixin does not inherit from `unittest.TestCase`, so unittest does not collect it on its
own; list `unittest.TestCase` after it in your test class. Each test calls `make_connector`
with a fresh temporary directory, and closes the connector afterwards when it has `close()`.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Sequence

from harness2.core import intake
from harness2.core.intake import RawTask, TaskConnector


def check_connector(connector: Any) -> None:
    """Assert that `connector` meets the task connector contract.

    It must be a `TaskConnector` (poll / fetch_files / deliver / fail), and
    `intake.capabilities(connector)` must accept its optional capabilities. A capability of the
    wrong type raises AssertionError whose message names the capability.
    """
    if not isinstance(connector, TaskConnector):
        missing = [
            name
            for name in ("poll", "fetch_files", "deliver", "fail")
            if not callable(getattr(connector, name, None))
        ]
        raise AssertionError(
            f"{type(connector).__name__} is not a TaskConnector; "
            f"missing methods: {', '.join(missing) or '(none)'}"
        )
    try:
        intake.capabilities(connector)
    except TypeError as exc:
        raise AssertionError(f"{type(connector).__name__}: {exc}") from exc


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_judge_result(result: Any) -> None:
    """Assert that `result` has the shape of a Judge's result.

    It must be None, or a dict with `n_passed` (int) and `n_criteria` (int), where
    `0 <= n_passed <= n_criteria`, and `all_pass` (bool) when that key is present. Other keys
    are allowed; the harness keeps them as raw data.
    """
    if result is None:
        return
    if not isinstance(result, Mapping):
        raise AssertionError(f"a judge must return None or a dict, got {type(result).__name__}")
    for key in ("n_passed", "n_criteria"):
        if key not in result:
            raise AssertionError(
                f"a judge result must have the key {key!r}; got keys {sorted(map(str, result))}"
            )
        if not _is_int(result[key]):
            raise AssertionError(
                f"a judge result's {key!r} must be an int, got {type(result[key]).__name__}"
            )
    if "all_pass" in result and not isinstance(result["all_pass"], bool):
        raise AssertionError(
            f"a judge result's 'all_pass' must be a bool, got {type(result['all_pass']).__name__}"
        )
    n_passed, n_criteria = result["n_passed"], result["n_criteria"]
    if not 0 <= n_passed <= n_criteria:
        raise AssertionError(
            f"a judge result needs 0 <= n_passed <= n_criteria, got n_passed={n_passed}, "
            f"n_criteria={n_criteria}"
        )


def check_fetched_files(dest: Path, task_id: str = "") -> None:
    """Assert that what `fetch_files(raw, dest)` left at `dest` is acceptable.

    A connector may leave `dest` missing (a task without attachments, for example). When `dest`
    exists it must be a directory, nothing under it may be a symbolic link, and every path under
    it must resolve to a place under `dest`.
    """
    dest = Path(dest)
    label = f"fetch_files() for task {task_id!r}" if task_id else "fetch_files()"
    if not os.path.lexists(dest):
        return
    if dest.is_symlink() or not dest.is_dir():
        raise AssertionError(f"{label} left {dest} as something other than a directory")
    root = dest.resolve()
    for dirpath, dirnames, filenames in os.walk(dest, followlinks=False):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            if path.is_symlink():
                raise AssertionError(f"{label} left a symbolic link at {path}")
            if not path.resolve().is_relative_to(root):
                raise AssertionError(f"{label} left {path}, which is outside {dest}")


class ConnectorContractTests:
    """A unittest mixin that checks a connector against the task connector contract.

    Subclass it together with `unittest.TestCase` and implement `make_connector(tmp)`. Override
    `make_run_dir(tmp, task_id)` to give the judge a richer run directory, and `judge_task_id`
    (set it to pass a fixed task_id to the judge instead of each polled task_id) /
    `judge_criteria` to change what the judge is called with.
    """

    judge_task_id: str = ""
    judge_criteria: Sequence[str] = ()

    def make_connector(self, tmp: Path) -> Any:
        """Build the connector under test; `tmp` is a fresh, empty temporary directory."""
        raise NotImplementedError("subclasses of ConnectorContractTests implement make_connector")

    def make_run_dir(self, tmp: Path, task_id: str) -> Path:
        """The run directory handed to the judge; by default a new, empty directory."""
        run_dir = tmp / "contract-runs" / task_id
        run_dir.mkdir(parents=True)
        return run_dir

    # -- helpers -------------------------------------------------------------------------

    def _tmp(self) -> Path:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)  # type: ignore[attr-defined]
        return Path(holder.name)

    def _make(self, tmp: Path) -> Any:
        root = tmp / "connector"
        root.mkdir()
        return self.make_connector(root)

    def _connector(self, tmp: Path) -> Any:
        conn = self._make(tmp)
        close = getattr(conn, "close", None)
        if callable(close):
            self.addCleanup(close)  # type: ignore[attr-defined]
        return conn

    # -- tests ---------------------------------------------------------------------------

    def test_contract_check_connector(self) -> None:
        tmp = self._tmp()
        check_connector(self._connector(tmp))

    def test_contract_poll_returns_raw_tasks(self) -> None:
        tmp = self._tmp()
        tasks = list(self._connector(tmp).poll())
        for task in tasks:
            if not isinstance(task, RawTask):
                raise AssertionError(f"poll() must yield RawTask, got {type(task).__name__}")

    def test_contract_fetch_files_stays_under_dest(self) -> None:
        tmp = self._tmp()
        conn = self._connector(tmp)
        for i, raw in enumerate(conn.poll()):
            dest = tmp / "contract-files" / str(i)
            conn.fetch_files(raw, dest)
            check_fetched_files(dest, task_id=raw.id)

    def test_contract_judge_result(self) -> None:
        tmp = self._tmp()
        conn = self._connector(tmp)
        judge = intake.capabilities(conn).judge
        if judge is None:
            return
        task_ids = [raw.id for raw in conn.poll()] or ["contract-task"]
        for task_id in task_ids:
            run_dir = self.make_run_dir(tmp, task_id)
            check_judge_result(
                judge(run_dir, self.judge_task_id or task_id, tuple(self.judge_criteria))
            )

    def test_contract_report_returns_text_or_none(self) -> None:
        tmp = self._tmp()
        conn = self._connector(tmp)
        report = intake.capabilities(conn).report
        if report is None:
            return
        task_ids = [raw.id for raw in conn.poll()] or ["contract-task"]
        for task_id in task_ids:
            text = report(task_id)
            if text is not None and not isinstance(text, str):
                raise AssertionError(
                    f"report({task_id!r}) must return str or None, got {type(text).__name__}"
                )

    def test_contract_close_does_not_raise(self) -> None:
        tmp = self._tmp()
        conn = self._make(tmp)
        intake.capabilities(conn).close()
