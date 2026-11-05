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

"""The Ledger's rollout money comes from the adapter's rollout cost."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness2.benchmarks import intake_worktree as iw
from harness2.benchmarks.intake import IntakeAdapter
from harness2.core import recursion, schedule, spec, tree
from harness2.demo import DemoAdapter, DemoProposer


class FixedCostAdapter:
    """An adapter that reports one fixed rollout cost (or raises / returns junk)."""

    def __init__(self, value):
        self.value = value

    def rollout_cost_usd(self, run_id, run_dir):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class RolloutCostUsdTest(unittest.TestCase):
    def test_an_adapter_without_the_method_costs_nothing(self):
        self.assertEqual(spec.rollout_cost_usd(object(), "r1", Path("run")), 0.0)

    def test_a_positive_number_is_the_cost(self):
        self.assertEqual(spec.rollout_cost_usd(FixedCostAdapter(1.25), "r1", Path("run")), 1.25)
        self.assertEqual(spec.rollout_cost_usd(FixedCostAdapter(2), "r1", Path("run")), 2.0)
        self.assertEqual(spec.rollout_cost_usd(FixedCostAdapter(0), "r1", Path("run")), 0.0)

    def test_bad_values_cost_nothing(self):
        for value in (-0.5, "1.25", True, False, None, float("nan"), float("inf")):
            with self.subTest(value=value):
                got = spec.rollout_cost_usd(FixedCostAdapter(value), "r1", Path("run"))
                self.assertEqual(got, 0.0)

    def test_an_int_too_large_for_a_float_costs_nothing(self):
        # `math.isfinite(10**400)` raises OverflowError if the check runs outside the try.
        got = spec.rollout_cost_usd(FixedCostAdapter(10**400), "r1", Path("run"))
        self.assertEqual(got, 0.0)

    def test_an_exception_costs_nothing(self):
        adapter = FixedCostAdapter(RuntimeError("events.jsonl is unreadable"))
        self.assertEqual(spec.rollout_cost_usd(adapter, "r1", Path("run")), 0.0)

    def test_a_mock_adapter_costs_nothing(self):
        self.assertEqual(spec.rollout_cost_usd(mock.Mock(), "r1", Path("run")), 0.0)


class PricedAdapter(DemoAdapter):
    """The demo adapter, with every rollout costing `cost`."""

    cost = 0.25

    def rollout_cost_usd(self, run_id, run_dir):
        return self.cost


class BatchPricedAdapter(PricedAdapter):
    def execute_many(self, task_id, harness_dir, run_ids):
        return [self.execute(task_id, harness_dir, run_id) for run_id in run_ids]


class FailingAdapter(PricedAdapter):
    cost = 0.4

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        raise RuntimeError("the rollout died")


class PricedProposer(DemoProposer):
    """The scripted proposer, with every session costing 0.5."""

    def call(self, mission, context_dir, label, **kwargs):
        reply = super().call(mission, context_dir, label, **kwargs)
        reply.cost_usd = 0.5
        return reply


class DemoBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adapter = PricedAdapter(self.root)
        self.prop = PricedProposer(self.adapter.spec.channels["proposer"])


class RecursionCostTest(DemoBase):
    def _run_mode(self, mode):
        surface = self.adapter.substrate.surface()
        base_tree = tree.read_tree(surface, self.adapter.base_harness())
        base_dir = self.adapter.execute("ledger-1", None, "base1")
        base = [
            spec.RunView(
                label="base1",
                run_id="base1",
                run_dir=base_dir,
                ok=True,
                delivered=True,
                cost_usd=self.adapter.cost,
            )
        ]
        return recursion.run_mode(
            mode,
            "ledger-1",
            1,
            base,
            adapter=self.adapter,
            prop=self.prop,
            work_dir=self.root / "work" / mode,
            run_id_of=lambda label, _mode=mode: f"{_mode}/{label}",
            base_tree=base_tree,
        )

    def test_rollout_usd_sums_every_rollout_including_the_base(self):
        for mode in ("agg", "seq"):
            with self.subTest(mode=mode):
                result = self._run_mode(mode)
                ledger = result.ledger
                self.assertIsNotNone(ledger)
                sum_of_rollouts = sum(rv.cost_usd for rv in result.rollouts.values())
                self.assertAlmostEqual(sum_of_rollouts, 4 * self.adapter.cost)
                self.assertAlmostEqual(ledger.rollout_usd, sum_of_rollouts)
                self.assertGreater(ledger.session_usd, 0.0)
                self.assertAlmostEqual(ledger.usd, ledger.rollout_usd + ledger.session_usd)


class RollBasesCostTest(DemoBase):
    def test_the_one_at_a_time_path_fills_the_cost(self):
        rolled = schedule._roll_bases(
            "ledger-1", self.adapter, [("base1", "base1")], self.root / "missing"
        )
        self.assertTrue(rolled["base1"].ok)
        self.assertAlmostEqual(rolled["base1"].cost_usd, self.adapter.cost)

    def test_the_batch_path_fills_the_cost(self):
        adapter = BatchPricedAdapter(self.root)
        rolled = schedule._roll_bases(
            "ledger-1", adapter, [("base1", "r1"), ("base2", "r2")], self.root / "missing"
        )
        for label in ("base1", "base2"):
            self.assertTrue(rolled[label].ok)
            self.assertAlmostEqual(rolled[label].cost_usd, adapter.cost)

    def test_a_dead_rollout_keeps_its_cost(self):
        adapter = FailingAdapter(self.root)
        rolled = schedule._roll_bases("ledger-1", adapter, [("base1", "base1")], self.root / "x")
        self.assertFalse(rolled["base1"].ok)
        self.assertAlmostEqual(rolled["base1"].cost_usd, adapter.cost)


class FakeConnector:
    def poll(self):
        return []

    def fetch_files(self, raw, dest):
        return None

    def deliver(self, task_id, deliverable_dir, summary):
        return None

    def fail(self, task_id, reason):
        return None


class FakeModelConnector:
    def __init__(self, by_task):
        self.by_task = by_task

    def routes(self, role, *, task_id, substrate):
        return list(self.by_task.get(task_id, []))

    def record(self, role, task_id, choice, outcome):
        return None


def _finish(cost):
    return {"type": "step_finish", "part": {"type": "step_finish", "cost": cost, "tokens": {}}}


def _write_events(path, *events):
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


class NothingIntake(IntakeAdapter):
    """An intake adapter that launches without a stored task spec (no task dir is written)."""

    def task_prompt(self, task_id):
        return "do it"

    def task_files(self, task_id):
        return None


class EventsLauncher:
    """Writes one step_finish per given cost to the run's events.jsonl, then maybe raises."""

    def __init__(self, *costs, error=None):
        self.costs = costs
        self.error = error
        self.calls = 0

    def launch(self, *, workspace, prompt, install, run_dir, timeout, task_id=""):
        self.calls += 1
        finishes = [_finish(cost) for cost in self.costs]
        if finishes:
            _write_events(Path(run_dir) / iw.EVENTS_NAME, *finishes)
        if self.error is not None:
            raise self.error


def _intake(root, launcher=None):
    return NothingIntake(
        root,
        FakeConnector(),
        object(),
        launcher=object() if launcher is None else launcher,
        channel=spec.ChannelSpec("opencode", "fake-model"),
    )


class IntakeCostTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "root"

    def test_it_sums_the_step_finish_costs(self):
        adapter = _intake(self.root, EventsLauncher(0.25, 0.5))
        run_dir = adapter.execute("t1", None, "r1")
        self.assertAlmostEqual(adapter.rollout_cost_usd("r1", run_dir), 0.75)

    def test_a_missing_file_costs_nothing(self):
        adapter = _intake(self.root, EventsLauncher())
        run_dir = adapter.execute("t1", None, "r1")
        self.assertEqual(adapter.rollout_cost_usd("r1", run_dir), 0.0)

    def test_a_dead_rollout_is_read_from_its_run_id_directory(self):
        # `_roll` hands a dead rollout `work_dir/"missing"`; the events of the attempt that
        # raised are still under `executions/<run_id>`.
        adapter = _intake(self.root, EventsLauncher(0.3, error=RuntimeError("boom")))
        with self.assertRaises(RuntimeError):
            adapter.execute("t1", None, "r1")
        self.assertAlmostEqual(
            adapter.rollout_cost_usd("r1", self.root / "elsewhere" / "missing"), 0.3
        )


class CachedRolloutCostTest(unittest.TestCase):
    """A run reused through `.complete` was not launched by this instance."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "root"

    def test_a_cached_run_costs_nothing(self):
        adapter = _intake(self.root)
        run_dir = adapter.root / "executions" / "r1"
        run_dir.mkdir(parents=True)
        _write_events(run_dir / iw.EVENTS_NAME, _finish(0.75))
        (run_dir / ".complete").write_text("r1\n", encoding="utf-8")
        self.assertEqual(adapter.execute("t1", None, "r1"), run_dir)
        self.assertEqual(adapter.rollout_cost_usd("r1", run_dir), 0.0)

    def test_the_same_instance_that_launched_it_costs_its_events(self):
        launcher = EventsLauncher(0.75)
        adapter = _intake(self.root, launcher)
        run_dir = adapter.execute("t1", None, "r1")
        self.assertEqual(launcher.calls, 1)
        self.assertAlmostEqual(adapter.rollout_cost_usd("r1", run_dir), 0.75)


class ScriptedAgent(iw.OpencodeAgent):
    """Fails the first attempt as unavailable, then succeeds; both attempts cost money."""

    def __init__(self, model):
        super().__init__(model)
        self.attempts = 0

    def _run_once(self, argv, run_dir, events, timeout, env):
        self.attempts += 1
        if self.attempts == 1:
            _write_events(
                events,
                {"type": "error", "error": {"name": "ProviderAuthError", "data": {}}},
                _finish(0.5),
            )
            return 3
        _write_events(events, _finish(0.25))
        return 0


class AgentLauncher:
    """Launches one run through a prepared OpencodeAgent, as production does."""

    def __init__(self, agent, worktree):
        self.agent = agent
        self.worktree = worktree

    def launch(self, *, workspace, prompt, install, run_dir, timeout, task_id=""):
        self.agent.run(
            worktree=self.worktree,
            prompt=prompt,
            install=install,
            run_dir=run_dir,
            timeout=timeout,
            env={},
            task_id=task_id,
        )


class RetriedRolloutCostTest(unittest.TestCase):
    def test_a_retried_rollout_matches_the_executor_record(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tmp = Path(temporary.name)
        connector = FakeModelConnector(
            {
                "t1": [
                    iw.models.ModelChoice("opencode", "bad"),
                    iw.models.ModelChoice("opencode", "good"),
                ]
            }
        )
        selector = iw.ExecutorModels(connector, name="fake")
        launcher = AgentLauncher(ScriptedAgent(selector), tmp / "worktree")
        adapter = _intake(tmp / "root", launcher)
        run_dir = adapter.execute("t1", None, "r1")
        # The failed attempt's events are archived as events.1.jsonl.
        self.assertTrue((run_dir / "events.1.jsonl").is_file())
        recorded = sum(row["cost_usd"] for row in selector.records_for("t1"))
        self.assertAlmostEqual(recorded, 0.75)
        self.assertAlmostEqual(adapter.rollout_cost_usd("r1", run_dir), recorded)


if __name__ == "__main__":
    unittest.main()
