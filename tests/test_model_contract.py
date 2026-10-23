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

"""The model connector contract checks of harness2.testing.model_contract."""

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from harness2.core import models
from harness2.testing.model_contract import (
    ModelConnectorContractTests,
    check_choices,
    check_model_connector,
    check_session_result,
)

FENCE = models.FenceDeclaration(**dataclasses.asdict(models.FENCE_REQUIREMENTS["claude"]))


def make_request(tmp: Path, choice: models.ModelChoice) -> models.SessionRequest:
    return models.SessionRequest(
        cwd=tmp, mission="say hello", system="be brief", wall=30, label="contract", choice=choice
    )


class CandidatesOnlyConnector:
    """A connector that only returns candidates."""

    def routes(self, role: str, *, task_id: str, substrate: str) -> list[models.ModelChoice]:
        if role == models.Role.EXECUTOR:
            return [models.ModelChoice(runner=substrate, model="fake/model-a")]
        return [models.ModelChoice(runner="claude_cli", model="fake/model-b")]


class SessionConnector:
    """A connector that runs sessions, declares a fence and records outcomes."""

    def __init__(self) -> None:
        self.sessions: list[tuple[str, models.SessionRequest]] = []
        self.records: list[tuple[str, str, models.ModelChoice, models.Outcome]] = []
        self.close_calls = 0

    def routes(self, role: str, *, task_id: str, substrate: str) -> list[models.ModelChoice]:
        if role == models.Role.EXECUTOR:
            return [models.ModelChoice(runner=substrate, model="fake/executor", effort="high")]
        return [models.ModelChoice(runner="claude", model="fake/improver")]

    def run_session(self, role: str, request: models.SessionRequest) -> models.SessionResult:
        self.sessions.append((role, request))
        return models.SessionResult(text="done", ok=True, cost_usd=0.5, wall_s=1.0)

    def fences(self) -> dict[str, models.FenceDeclaration]:
        return {"claude": FENCE}

    def record(
        self, role: str, task_id: str, choice: models.ModelChoice, outcome: models.Outcome
    ) -> None:
        self.records.append((role, task_id, choice, outcome))

    def close(self) -> None:
        self.close_calls += 1


class LooseFenceConnector:
    """A connector with run_session but a fence harness2 does not accept."""

    def routes(self, role: str, *, task_id: str, substrate: str) -> list[models.ModelChoice]:
        if role == models.Role.EXECUTOR:
            return []
        return [models.ModelChoice(runner="claude", model="fake/improver")]

    def run_session(self, role: str, request: models.SessionRequest) -> models.SessionResult:
        return models.SessionResult(text="done", ok=True)

    def fences(self) -> dict[str, models.FenceDeclaration]:
        return {"claude": dataclasses.replace(FENCE, restricted=False)}


class EnvConnectorContractTest(ModelConnectorContractTests, unittest.TestCase):
    """The default connector against the model connector contract."""

    def make_connector(self) -> models.ModelConnector:
        return models.EnvModelConnector()


class CandidatesOnlyContractTest(ModelConnectorContractTests, unittest.TestCase):
    """A connector that returns candidates only against the model connector contract."""

    def make_connector(self) -> models.ModelConnector:
        return CandidatesOnlyConnector()


class SessionConnectorContractTest(ModelConnectorContractTests, unittest.TestCase):
    """A connector with run_session, fences, record and close against the contract."""

    def make_connector(self) -> models.ModelConnector:
        return SessionConnector()

    def make_session_request(self, tmp: Path, choice: models.ModelChoice) -> models.SessionRequest:
        return make_request(tmp, choice)


class SessionCheckTest(unittest.TestCase):
    """`test_model_contract_run_session` calls run_session once with the built request."""

    def test_the_session_check_calls_run_session(self) -> None:
        connector = SessionConnector()

        class ContractTest(ModelConnectorContractTests, unittest.TestCase):
            def make_connector(self) -> models.ModelConnector:
                return connector

            def make_session_request(
                self, tmp: Path, choice: models.ModelChoice
            ) -> models.SessionRequest:
                return make_request(tmp, choice)

        case = ContractTest("test_model_contract_run_session")
        case.test_model_contract_run_session()
        self.assertEqual([role for role, _ in connector.sessions], [models.Role.IMPROVER])
        request = connector.sessions[0][1]
        self.assertEqual(request.mission, "say hello")
        self.assertEqual(request.choice, models.ModelChoice(runner="claude", model="fake/improver"))
        self.assertTrue(request.cwd.is_dir())
        case.doCleanups()


class FenceCheckTest(unittest.TestCase):
    """`test_model_contract_fences` rejects run_session without a fence harness2 accepts."""

    def test_a_loose_fence_fails_with_the_fence_report(self) -> None:
        class ContractTest(ModelConnectorContractTests, unittest.TestCase):
            def make_connector(self) -> models.ModelConnector:
                return LooseFenceConnector()

        case = ContractTest("test_model_contract_fences")
        with self.assertRaises(AssertionError) as caught:
            case.test_model_contract_fences()
        case.doCleanups()
        self.assertIn("restricted", str(caught.exception))

    def test_a_connector_without_run_session_does_not_check_fences(self) -> None:
        class ContractTest(ModelConnectorContractTests, unittest.TestCase):
            def make_connector(self) -> models.ModelConnector:
                return CandidatesOnlyConnector()

        case = ContractTest("test_model_contract_fences")
        self.assertIsNone(case.test_model_contract_fences())
        case.doCleanups()


class SessionSkipTest(unittest.TestCase):
    """`test_model_contract_run_session` skips when it cannot run a session."""

    def test_no_accepted_runner_skips(self) -> None:
        class ContractTest(ModelConnectorContractTests, unittest.TestCase):
            def make_connector(self) -> models.ModelConnector:
                return LooseFenceConnector()

            def make_session_request(
                self, tmp: Path, choice: models.ModelChoice
            ) -> models.SessionRequest:
                return make_request(tmp, choice)

        case = ContractTest("test_model_contract_run_session")
        with self.assertRaises(unittest.SkipTest) as caught:
            case.test_model_contract_run_session()
        case.doCleanups()
        self.assertIn("fence", str(caught.exception))

    def test_missing_make_session_request_skips(self) -> None:
        class ContractTest(ModelConnectorContractTests, unittest.TestCase):
            def make_connector(self) -> models.ModelConnector:
                return SessionConnector()

        case = ContractTest("test_model_contract_run_session")
        with self.assertRaises(unittest.SkipTest) as caught:
            case.test_model_contract_run_session()
        case.doCleanups()
        self.assertIn("make_session_request", str(caught.exception))


class CloseCheckTest(unittest.TestCase):
    """`test_model_contract_close_is_optional_and_callable` calls close twice."""

    def test_the_close_check_calls_close_twice(self) -> None:
        connector = SessionConnector()

        class ContractTest(ModelConnectorContractTests, unittest.TestCase):
            def make_connector(self) -> models.ModelConnector:
                return connector

        case = ContractTest("test_model_contract_close_is_optional_and_callable")
        case.test_model_contract_close_is_optional_and_callable()
        case.doCleanups()
        self.assertEqual(connector.close_calls, 2)


class WrongExecutorRunnerConnector(CandidatesOnlyConnector):
    """A connector whose executor candidates run somewhere other than the substrate."""

    def routes(self, role: str, *, task_id: str, substrate: str) -> list[models.ModelChoice]:
        return [models.ModelChoice(runner="codex", model="fake/model-a")]


class SessionWithoutFencesConnector:
    """A connector with run_session but without the fences that go with it."""

    def routes(self, role: str, *, task_id: str, substrate: str) -> list[models.ModelChoice]:
        return []

    def run_session(self, role: str, request: models.SessionRequest) -> models.SessionResult:
        return models.SessionResult(text="done", ok=True)


class CheckModelConnectorTest(unittest.TestCase):
    def test_env_connector_passes(self) -> None:
        self.assertIsNone(check_model_connector(models.EnvModelConnector()))

    def test_object_without_routes_names_routes(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_model_connector(object())
        self.assertIn("routes", str(caught.exception))

    def test_routes_not_callable_names_routes(self) -> None:
        conn = CandidatesOnlyConnector()
        conn.routes = 5  # type: ignore[method-assign]
        with self.assertRaises(AssertionError) as caught:
            check_model_connector(conn)
        self.assertIn("routes", str(caught.exception))

    def test_run_session_without_fences_names_fences(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_model_connector(SessionWithoutFencesConnector())
        self.assertIn("fences", str(caught.exception))

    def test_optional_member_that_is_not_callable_names_it(self) -> None:
        conn = CandidatesOnlyConnector()
        conn.record = 5  # type: ignore[attr-defined]
        with self.assertRaises(AssertionError) as caught:
            check_model_connector(conn)
        self.assertIn("record", str(caught.exception))


class CheckChoicesTest(unittest.TestCase):
    def test_empty_list_passes_for_both_roles(self) -> None:
        check_choices([], role=models.Role.EXECUTOR, substrate="opencode")
        check_choices([], role=models.Role.IMPROVER, substrate="opencode")

    def test_executor_candidate_on_the_substrate_passes(self) -> None:
        choices = [models.ModelChoice(runner="opencode", model="m", effort="high")]
        check_choices(choices, role=models.Role.EXECUTOR, substrate="opencode")

    def test_improver_candidate_may_use_any_runner(self) -> None:
        check_choices(
            [models.ModelChoice("claude", "m")], role=models.Role.IMPROVER, substrate="opencode"
        )

    def test_not_a_list(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_choices(
                (models.ModelChoice("opencode", "m"),),
                role=models.Role.EXECUTOR,
                substrate="opencode",
            )
        self.assertIn("list", str(caught.exception))

    def test_element_that_is_not_a_model_choice(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_choices([("opencode", "m")], role=models.Role.IMPROVER, substrate="opencode")
        self.assertIn("ModelChoice", str(caught.exception))

    def test_empty_runner(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_choices(
                [models.ModelChoice("", "m")], role=models.Role.IMPROVER, substrate="opencode"
            )
        self.assertIn("runner", str(caught.exception))

    def test_empty_model(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_choices(
                [models.ModelChoice("opencode", "")],
                role=models.Role.IMPROVER,
                substrate="opencode",
            )
        self.assertIn("model", str(caught.exception))

    def test_effort_that_is_not_a_str(self) -> None:
        choice = models.ModelChoice("opencode", "m")
        choice = dataclasses.replace(choice, effort=1)  # type: ignore[arg-type]
        with self.assertRaises(AssertionError) as caught:
            check_choices([choice], role=models.Role.IMPROVER, substrate="opencode")
        self.assertIn("effort", str(caught.exception))

    def test_executor_runner_that_is_not_the_substrate(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_choices(
                WrongExecutorRunnerConnector().routes(
                    models.Role.EXECUTOR, task_id="t", substrate="opencode"
                ),
                role=models.Role.EXECUTOR,
                substrate="opencode",
            )
        self.assertIn("substrate", str(caught.exception))


class CheckSessionResultTest(unittest.TestCase):
    def test_ok_result_passes(self) -> None:
        check_session_result(models.SessionResult(text="done", ok=True, cost_usd=1.5, wall_s=2))

    def test_unavailable_result_passes(self) -> None:
        check_session_result(models.SessionResult(text="", ok=False, unavailable=True))

    def test_not_a_session_result(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_session_result(object())
        self.assertIn("SessionResult", str(caught.exception))

    def test_ok_with_empty_text(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_session_result(models.SessionResult(text="", ok=True))
        self.assertIn("text", str(caught.exception))

    def test_ok_and_unavailable(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_session_result(models.SessionResult(text="done", ok=True, unavailable=True))
        self.assertIn("unavailable", str(caught.exception))

    def test_negative_cost(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_session_result(models.SessionResult(text="done", ok=True, cost_usd=-1))
        self.assertIn("cost_usd", str(caught.exception))

    def test_negative_wall(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            check_session_result(models.SessionResult(text="done", ok=True, wall_s=-1))
        self.assertIn("wall_s", str(caught.exception))

    def test_runner_that_is_not_a_str(self) -> None:
        result = models.SessionResult(text="done", ok=True, runner=3)  # type: ignore[arg-type]
        with self.assertRaises(AssertionError) as caught:
            check_session_result(result)
        self.assertIn("runner", str(caught.exception))

    def test_runner_str_passes(self) -> None:
        check_session_result(models.SessionResult(text="done", ok=True, runner="claude"))

    def test_text_that_is_not_a_str(self) -> None:
        result = models.SessionResult(text=1, ok=True)  # type: ignore[arg-type]
        with self.assertRaises(AssertionError) as caught:
            check_session_result(result)
        self.assertIn("text", str(caught.exception))


class FakeConnectorTest(unittest.TestCase):
    """The fake connectors themselves honor the members the mixin tests read."""

    def test_candidates_only_connector_has_no_optional_members(self) -> None:
        caps = models.capabilities(CandidatesOnlyConnector())
        self.assertIsNone(caps.run_session)
        self.assertIsNone(caps.record)
        self.assertEqual(caps.fences(), {})

    def test_session_connector_fences_a_runner_harness2_accepts(self) -> None:
        self.assertEqual(models.fenced_runners(SessionConnector()), {"claude": FENCE})


if __name__ == "__main__":
    unittest.main()
