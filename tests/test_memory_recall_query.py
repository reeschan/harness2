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

"""The optional task prompt (`query`) passed from run_task to a connector's recall."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_connectors import InMemoryConnector

from harness2.core import memory, schedule
from harness2.demo import DemoAdapter, DemoProposer

DOMAIN = "accounting"
K = 1
TASK = "ledger-1"


class TwoArgumentRecall:
    """The original recall shape: exactly two positional arguments."""

    def __init__(self):
        self.calls = []

    def recall(self, task_id, limit_chars):
        self.calls.append((task_id, limit_chars))
        return "two-argument"


class QueryKeywordRecall:
    """A recall that takes the task prompt as a keyword-only `query`."""

    def __init__(self, text="ok"):
        self.calls = []
        self.text = text

    def recall(self, task_id, limit_chars, *, query=""):
        self.calls.append((task_id, limit_chars, query))
        return self.text


class QueryKwargsRecall:
    """A recall that takes the task prompt through `**kwargs`."""

    def __init__(self):
        self.calls = []

    def recall(self, task_id, limit_chars, **kwargs):
        self.calls.append((task_id, limit_chars, dict(kwargs)))
        return "kwargs"


class UnreadableSignature:
    """A callable whose signature inspect.signature cannot read."""

    def __init__(self):
        self.calls = []

    @property
    def __signature__(self):
        raise ValueError("no signature")

    def __call__(self, task_id, limit_chars):
        self.calls.append((task_id, limit_chars))
        return "unreadable"


class OpaqueRecall:
    """A connector whose recall callable has no readable signature."""

    def __init__(self):
        self.recall = UnreadableSignature()


class MemoryRecallQueryTests(unittest.TestCase):
    def test_keyword_recall_gets_the_query(self):
        conn = QueryKeywordRecall()
        self.assertEqual(memory.recall(conn, "task-1", 100, query="fix the ledger"), "ok")
        self.assertEqual(conn.calls, [("task-1", 100, "fix the ledger")])

    def test_query_defaults_to_empty(self):
        conn = QueryKeywordRecall()
        memory.recall(conn, "task-1", 100)
        self.assertEqual(conn.calls, [("task-1", 100, "")])

    def test_kwargs_recall_gets_the_query(self):
        conn = QueryKwargsRecall()
        memory.recall(conn, "task-2", 50, query="sum the amounts")
        self.assertEqual(conn.calls, [("task-2", 50, {"query": "sum the amounts"})])

    def test_two_argument_recall_is_called_the_old_way(self):
        conn = TwoArgumentRecall()
        self.assertEqual(memory.recall(conn, "task-3", 50, query="ignored"), "two-argument")
        self.assertEqual(conn.calls, [("task-3", 50)])

    def test_unreadable_signature_takes_no_query(self):
        conn = OpaqueRecall()
        self.assertEqual(memory.recall(conn, "task-4", 40, query="ignored"), "unreadable")
        self.assertEqual(conn.recall.calls, [("task-4", 40)])

    def test_result_is_cut_to_the_limit(self):
        conn = QueryKeywordRecall(text="x" * 500)
        self.assertEqual(memory.recall(conn, "task-5", 7, query="q"), "x" * 7)


class RecordingConnector(InMemoryConnector):
    """InMemoryConnector that records the `query` every recall is called with."""

    def __init__(self, root, domain, surface=None):
        super().__init__(root, domain, surface)
        self.queries = []

    def recall(self, task_id, limit_chars, *, query=""):
        self.queries.append(query)
        return super().recall(task_id, limit_chars)


class ScheduleRecallQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        InMemoryConnector.reset()
        self.addCleanup(InMemoryConnector.reset)
        self.adapter = DemoAdapter(self.root / "demo")
        self.proposer = DemoProposer(self.adapter.spec.channels["proposer"])
        self.surface = self.adapter.substrate.surface()

    def run_single_mode(self, conn):
        """One run_task over the agg mode; the mode body itself is patched out."""
        with patch.object(schedule, "_run_one_mode"):
            return schedule.run_task(
                TASK,
                K,
                adapter=self.adapter,
                prop=self.proposer,
                runs_root=self.adapter.spec.runs_root,
                libraries={"agg": conn},
                modes=("agg",),
                memory_recall=True,
            )

    def test_run_task_passes_the_adapter_prompt(self):
        conn = RecordingConnector(self.root / "lib", DOMAIN, self.surface)
        prompt = self.adapter.task_prompt(TASK)
        res = self.run_single_mode(conn)
        self.assertEqual(conn.queries, [prompt])
        self.assertIn("agg", res.memory_recall)

    def test_task_prompt_error_still_recalls_with_an_empty_query(self):
        conn = RecordingConnector(self.root / "raised", DOMAIN, self.surface)
        with patch.object(self.adapter, "task_prompt", side_effect=RuntimeError("no prompt")):
            res = self.run_single_mode(conn)
        self.assertEqual(conn.queries, [""])
        self.assertIn("agg", res.memory_recall)


if __name__ == "__main__":
    unittest.main()
