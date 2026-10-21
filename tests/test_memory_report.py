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

"""The memory settings line harness2-report reads from the task summaries."""

import json
import tempfile
import unittest
from pathlib import Path

from harness2.cli import report


class MemorySettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ns_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _summary(self, task: str, body: object) -> None:
        path = self.ns_dir / task / "summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body if isinstance(body, str) else json.dumps(body))

    def test_no_keys_is_empty(self) -> None:
        self._summary("t1", {"modes": {}})
        self._summary("t2", {"selections": {}})
        self.assertEqual(report._memory_settings(self.ns_dir), set())

    def test_no_summaries_is_empty(self) -> None:
        self.assertEqual(report._memory_settings(self.ns_dir), set())

    def test_pair_is_returned(self) -> None:
        self._summary("t1", {"memory_connector": "x:Y", "memory_scope": "team1"})
        self._summary("t2", {"memory_connector": "x:Y", "memory_scope": "team1"})
        self._summary("t3", {"modes": {}})
        self.assertEqual(report._memory_settings(self.ns_dir), {("x:Y", "team1")})

    def test_one_key_defaults_the_other(self) -> None:
        self._summary("t1", {"memory_connector": "x:Y"})
        self._summary("t2", {"memory_scope": "team1"})
        self.assertEqual(report._memory_settings(self.ns_dir), {("x:Y", ""), ("file", "team1")})

    def test_unreadable_summary_is_skipped(self) -> None:
        self._summary("bad", "{not json")
        self._summary("list", [1, 2])
        self._summary("t1", {"memory_connector": "x:Y", "memory_scope": "team1"})
        self.assertEqual(report._memory_settings(self.ns_dir), {("x:Y", "team1")})

    def test_line_names_each_pair(self) -> None:
        line = report._memory_line({("x:Y", "team1"), ("x:Y", "")})
        self.assertEqual(line, "memory: connector=x:Y scope=(per run); connector=x:Y scope=team1")


if __name__ == "__main__":
    unittest.main()
