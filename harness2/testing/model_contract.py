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

"""Contract checks for model connectors, for connector authors to run from their own tests.

The model connector contract is described in `harness2.core.models`: a connector has a
callable `routes(role, *, task_id, substrate)`, which returns the `ModelChoice` candidates for
a role (`executor` or `improver`), best first, plus four optional capabilities read through
`models`: `run_session(role, request)`, `fences()`, `record(role, task_id, choice, outcome)`
and `close()`. A connector that defines `run_session` must also define `fences`, because
harness2 checks the fence of a runner before it hands a session over. The executor's runner is
the substrate harness2 fixed; the connector chooses only the model, the effort and the order of
the candidates.

- `check_model_connector(conn)` raises AssertionError when `conn` is not a `ModelConnector`
  (a callable `routes`), or when `models.capabilities(conn)` rejects an optional member: a
  member that is present but not callable, or `run_session` without `fences`. The message names
  the missing or invalid members.
- `check_choices(choices, *, role, substrate)` raises AssertionError when `choices` is not a
  list of `ModelChoice`, when a candidate's `runner` or `model` is not a non-empty str or its
  `effort` is not a str, or when `role` is `executor` and a runner is not `substrate`.
- `check_session_result(result)` raises AssertionError when `result` is not a `SessionResult`,
  when `ok` is true and `text` is empty, when `unavailable` and `ok` are both true, or when
  `cost_usd` or `wall_s` is negative.
- `ModelConnectorContractTests` is a unittest mixin that runs these checks, and a few more,
  against the connector your test builds: `check_model_connector`; `routes` for the executor
  (substrate `"opencode"`) and for the improver both pass `check_choices` (an empty list is
  fine); a `fences()` that is present is read through `models.fenced_runners`, and a connector
  that has both `run_session` and `fences()` must leave at least one accepted runner there, or
  the check fails with `models.fence_report` in the message; `run_session` (if any) is called
  once, with a request from `make_session_request(tmp, choice)` (`choice` is the first improver
  candidate on the first fenced runner; the check is skipped when there is no accepted runner,
  no such candidate, or `make_session_request` is not implemented), and passes
  `check_session_result`; `record` (if any) accepts `Outcome(ok=True)`; `close` is optional,
  and calling it twice does not raise.

Example::

    import unittest
    from pathlib import Path

    from harness2.core.models import ModelChoice, SessionRequest
    from harness2.testing.model_contract import ModelConnectorContractTests
    from my_package.models import MyConnector


    class MyConnectorContractTest(ModelConnectorContractTests, unittest.TestCase):
        def make_connector(self):
            return MyConnector()

        def make_session_request(self, tmp: Path, choice: ModelChoice) -> SessionRequest:
            return SessionRequest(
                cwd=tmp,
                mission="say hello",
                system="be brief",
                wall=30,
                label="contract",
                choice=choice,
            )

The mixin does not inherit from `unittest.TestCase`, so unittest does not collect it on its
own; list `unittest.TestCase` after it in your test class. Each test calls `make_connector()`
with no arguments. A connector without `run_session` skips the session check, and so does a
test that does not implement `make_session_request`; a connector with `run_session` but no
runner whose fence harness2 accepts fails the fence check instead.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from harness2.core import models

_SUBSTRATE = "opencode"
_TASK_ID = "contract"


def check_model_connector(conn: Any) -> None:
    """Assert that `conn` meets the model connector contract.

    `routes` must be callable, and `models.capabilities(conn)` must accept the optional
    members: `run_session`, `fences` and `record` must be methods (or missing/None), `close`
    must be callable when it is present, and a connector with `run_session` must also have
    `fences`. A missing or invalid member raises AssertionError whose message names it.
    """
    problems = [name for name in ("routes",) if not callable(getattr(conn, name, None))]
    if problems or not isinstance(conn, models.ModelConnector):
        detail = ", ".join(problems) or "the ModelConnector Protocol check failed"
        raise AssertionError(
            f"{type(conn).__name__} is not a ModelConnector; missing or invalid: {detail}"
        )
    try:
        models.capabilities(conn)
    except TypeError as exc:
        raise AssertionError(f"{type(conn).__name__}: {exc}") from exc


def check_choices(choices: Any, *, role: str, substrate: str) -> None:
    """Assert that `choices` is what `routes(role, substrate=substrate)` must return.

    It must be a list of `ModelChoice`; each candidate needs a non-empty str `runner`, a
    non-empty str `model` and a str `effort`, and, for the executor role, a `runner` equal to
    `substrate`. An empty list is fine.
    """
    if not isinstance(choices, list):
        raise AssertionError(f"routes({role!r}) must return a list, got {type(choices).__name__}")
    for index, choice in enumerate(choices):
        where = f"routes({role!r})[{index}]"
        if not isinstance(choice, models.ModelChoice):
            raise AssertionError(f"{where} must be a ModelChoice, got {type(choice).__name__}")
        for name in ("runner", "model"):
            value = getattr(choice, name)
            if not isinstance(value, str) or not value:
                raise AssertionError(f"{where}.{name} must be a non-empty str, got {value!r}")
        if not isinstance(choice.effort, str):
            raise AssertionError(
                f"{where}.effort must be a str, got {type(choice.effort).__name__}"
            )
        if role == models.Role.EXECUTOR and choice.runner != substrate:
            raise AssertionError(
                f"{where}.runner must be the substrate {substrate!r} for the executor role, "
                f"got {choice.runner!r}"
            )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_session_result(result: Any) -> None:
    """Assert that `result` has the shape a connector's `run_session` must return.

    It must be a `SessionResult` whose `text` is a str and non-empty when `ok` is true, whose
    `unavailable` is false when `ok` is true, and whose `cost_usd` and `wall_s` are numbers
    greater than or equal to zero, and whose `runner` is a str.
    """
    if not isinstance(result, models.SessionResult):
        raise AssertionError(
            f"run_session() must return a SessionResult, got {type(result).__name__}"
        )
    if not isinstance(result.text, str):
        raise AssertionError(
            f"a SessionResult's text must be a str, got {type(result.text).__name__}"
        )
    if not isinstance(result.runner, str):
        raise AssertionError(
            f"a SessionResult's runner must be a str, got {type(result.runner).__name__}"
        )
    if result.ok and not result.text:
        raise AssertionError("a SessionResult with ok=True must carry a non-empty text")
    if result.unavailable and result.ok:
        raise AssertionError("a SessionResult cannot be ok and unavailable at the same time")
    for name in ("cost_usd", "wall_s"):
        value = getattr(result, name)
        if not _is_number(value) or value < 0:
            raise AssertionError(
                f"a SessionResult's {name} must be a number greater than or equal to 0, "
                f"got {value!r}"
            )


class ModelConnectorContractTests:
    """A unittest mixin that checks a connector against the model connector contract.

    Subclass it together with `unittest.TestCase` and implement `make_connector()`. Implement
    `make_session_request(tmp, choice)` too when the connector has `run_session`; the session
    check is skipped without it. A connector with `run_session` must declare a runner whose
    fence harness2 accepts, or the fence check fails. Each test builds its own connector, and
    closes it afterwards when it has `close()`.
    """

    def make_connector(self) -> models.ModelConnector:
        """Build the connector under test."""
        raise NotImplementedError(
            "subclasses of ModelConnectorContractTests implement make_connector"
        )

    def make_session_request(self, tmp: Path, choice: models.ModelChoice) -> models.SessionRequest:
        """The request handed to `run_session`; `tmp` is a fresh, empty temporary directory and
        `choice` is the candidate to run (put it in `SessionRequest.choice`)."""
        raise NotImplementedError(
            "subclasses of ModelConnectorContractTests that test run_session implement "
            "make_session_request"
        )

    # -- helpers -------------------------------------------------------------------------

    def _tmp(self) -> Path:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)  # type: ignore[attr-defined]
        return Path(holder.name)

    def _connector(self) -> Any:
        conn = self.make_connector()
        close = getattr(conn, "close", None)
        if callable(close):
            self.addCleanup(close)  # type: ignore[attr-defined]
        return conn

    def _choices(self, conn: Any, role: str) -> list[models.ModelChoice]:
        return conn.routes(role, task_id=_TASK_ID, substrate=_SUBSTRATE)

    # -- tests ---------------------------------------------------------------------------

    def test_model_contract_check_model_connector(self) -> None:
        check_model_connector(self._connector())

    def test_model_contract_routes(self) -> None:
        conn = self._connector()
        for role in (models.Role.EXECUTOR, models.Role.IMPROVER):
            check_choices(self._choices(conn, role), role=role, substrate=_SUBSTRATE)

    def test_model_contract_fences(self) -> None:
        conn = self._connector()
        if not callable(getattr(conn, "fences", None)):
            return
        fenced = models.fenced_runners(conn)
        if fenced or not callable(getattr(conn, "run_session", None)):
            return
        report = models.fence_report(conn)
        detail = "; ".join(
            f"{runner}: {', '.join(problems) or 'accepted'}" for runner, problems in report.items()
        )
        raise AssertionError(
            "the connector has run_session but declares no runner whose fence harness2 "
            f"accepts ({detail or 'no fences declared'})"
        )

    def test_model_contract_run_session(self) -> None:
        conn = self._connector()
        run_session = getattr(conn, "run_session", None)
        if not callable(run_session):
            return
        fenced = models.fenced_runners(conn)
        if not fenced:
            self.skipTest(  # type: ignore[attr-defined]
                "the connector has run_session but no runner whose fence harness2 accepts"
            )
        runner = next(iter(fenced))
        choice = next(
            (c for c in self._choices(conn, models.Role.IMPROVER) if c.runner == runner), None
        )
        if choice is None:
            reason = f"no improver candidate runs on the fenced runner {runner!r}"
            self.skipTest(reason)  # type: ignore[attr-defined]
        try:
            request = self.make_session_request(self._tmp(), choice)
        except NotImplementedError:
            self.skipTest(  # type: ignore[attr-defined]
                "make_session_request is not implemented, so the session check is skipped"
            )
        check_session_result(run_session(models.Role.IMPROVER, request))

    def test_model_contract_record(self) -> None:
        conn = self._connector()
        record = getattr(conn, "record", None)
        if not callable(record):
            return
        choices = self._choices(conn, models.Role.IMPROVER)
        choice = choices[0] if choices else models.ModelChoice(runner=_SUBSTRATE, model="contract")
        record(models.Role.IMPROVER, _TASK_ID, choice, models.Outcome(ok=True))

    def test_model_contract_close_is_optional_and_callable(self) -> None:
        conn = self.make_connector()
        close = getattr(conn, "close", None)
        if callable(close):
            close()
            close()
