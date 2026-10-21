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

"""The connector contract checks of harness2.testing.connector_contract."""

import tempfile
import unittest
from pathlib import Path
from typing import Iterable, Mapping

from harness2.core.intake import RawTask, WorkspacePolicy
from harness2.core.intake_local import LocalFileConnector
from harness2.testing.connector_contract import (
    ConnectorContractTests,
    check_connector,
    check_fetched_files,
    check_judge_result,
)


class LocalConnectorWithoutFilesContractTest(ConnectorContractTests, unittest.TestCase):
    """A local task with no attachments: fetch_files leaves dest missing, which is fine."""

    def make_connector(self, tmp: Path) -> LocalFileConnector:
        folder = tmp / "inbox" / "t1"
        folder.mkdir(parents=True)
        (folder / "task.md").write_text("# A task\n\nDo it.\n", encoding="utf-8")
        return LocalFileConnector(tmp)

    def test_the_task_is_polled_and_dest_is_not_created(self) -> None:
        tmp = self._tmp()
        conn = self._connector(tmp)
        tasks = list(conn.poll())
        self.assertEqual([raw.id for raw in tasks], ["t1"])
        dest = tmp / "dest"
        conn.fetch_files(tasks[0], dest)
        self.assertFalse(dest.exists())
        check_fetched_files(dest)


class LocalConnectorWithFilesContractTest(ConnectorContractTests, unittest.TestCase):
    """A local task with one attachment."""

    def make_connector(self, tmp: Path) -> LocalFileConnector:
        folder = tmp / "inbox" / "t2"
        (folder / "files").mkdir(parents=True)
        (folder / "task.md").write_text("# A task\n\nDo it.\n", encoding="utf-8")
        (folder / "files" / "a.txt").write_text("a", encoding="utf-8")
        return LocalFileConnector(tmp)

    def test_the_attachment_is_fetched(self) -> None:
        tmp = self._tmp()
        conn = self._connector(tmp)
        tasks = list(conn.poll())
        self.assertEqual([raw.id for raw in tasks], ["t2"])
        dest = tmp / "dest"
        conn.fetch_files(tasks[0], dest)
        self.assertEqual((dest / "a.txt").read_text(encoding="utf-8"), "a")


class FullConnector:
    """A connector with every optional capability."""

    workspace_policy = WorkspacePolicy(exclude=("notes",), guard=lambda worktree, source: None)
    scrub_env = ("FAKE_API_KEY",)

    def __init__(self) -> None:
        self.closed = 0

    def poll(self) -> Iterable[RawTask]:
        return [RawTask(id="f1", title="T", body="B")]

    def fetch_files(self, raw: RawTask, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)

    def deliver(self, task_id: str, deliverable_dir: Path, summary: Mapping) -> None:
        return None

    def fail(self, task_id: str, reason: str) -> None:
        return None

    def judge(self):
        def run(run_dir: Path, report: str, criteria) -> dict:
            assert run_dir.is_dir()
            return {"n_passed": 1, "n_criteria": 2, "all_pass": False}

        return run

    def report(self, task_id: str) -> str:
        return f"{task_id}: DONE"

    def close(self) -> None:
        self.closed += 1


class FullConnectorContractTest(ConnectorContractTests, unittest.TestCase):
    def make_connector(self, tmp: Path) -> FullConnector:
        return FullConnector()


class BadScrubEnv(FullConnector):
    scrub_env = 5  # type: ignore[assignment]


class NotAConnector:
    def poll(self) -> Iterable[RawTask]:
        return []


class CheckFunctionsTest(unittest.TestCase):
    def test_wrong_capability_type_is_an_assertion_error_naming_it(self) -> None:
        with self.assertRaises(AssertionError) as ctx:
            check_connector(BadScrubEnv())
        self.assertIn("scrub_env", str(ctx.exception))

    def test_not_a_connector_is_an_assertion_error(self) -> None:
        with self.assertRaises(AssertionError) as ctx:
            check_connector(NotAConnector())
        self.assertIn("fetch_files", str(ctx.exception))

    def test_judge_result_with_other_keys_is_an_assertion_error(self) -> None:
        with self.assertRaises(AssertionError):
            check_judge_result({"passed": 1, "total": 2})

    def test_bad_judge_results(self) -> None:
        for bad in (
            [1, 2],
            {"n_passed": True, "n_criteria": 2},
            {"n_passed": 1, "n_criteria": "2"},
            {"n_passed": 3, "n_criteria": 2},
            {"n_passed": 1, "n_criteria": 2, "all_pass": 1},
        ):
            with self.subTest(result=bad):
                with self.assertRaises(AssertionError):
                    check_judge_result(bad)

    def test_fetched_files_missing_or_plain_dest_is_fine(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            check_fetched_files(root / "missing")
            (root / "dest" / "sub").mkdir(parents=True)
            (root / "dest" / "sub" / "a.txt").write_text("a", encoding="utf-8")
            check_fetched_files(root / "dest")

    def test_fetched_files_dest_that_is_a_file_is_an_assertion_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "dest"
            dest.write_text("x", encoding="utf-8")
            with self.assertRaises(AssertionError):
                check_fetched_files(dest)

    def test_fetched_files_with_a_symlink_is_an_assertion_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "outside.txt").write_text("x", encoding="utf-8")
            dest = root / "dest"
            dest.mkdir()
            (dest / "link").symlink_to(root / "outside.txt")
            with self.assertRaises(AssertionError) as ctx:
                check_fetched_files(dest, task_id="t1")
            self.assertIn("symbolic link", str(ctx.exception))

    def test_good_judge_results(self) -> None:
        for good in (
            None,
            {"n_passed": 0, "n_criteria": 0},
            {"n_passed": 2, "n_criteria": 2, "all_pass": True, "detail": "kept raw"},
        ):
            with self.subTest(result=good):
                check_judge_result(good)


if __name__ == "__main__":
    unittest.main()
