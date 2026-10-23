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

"""The connector channel: improver sessions run by a model connector's run_session."""

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness2.benchmarks.intake import _set_improver_task
from harness2.cli import intake as intake_cli
from harness2.core import models
from harness2.core.proposer import (
    _DROP_ENV,
    CLAUDE_CLI_PERMISSION_MODE,
    CLAUDE_CLI_TOOLS,
    ModelSession,
    Proposer,
    ProposerError,
)
from harness2.core.spec import ChannelSpec, resolve_channel_model

CONNECTOR_SPEC = ChannelSpec("connector", "")


def claude_fence(**changes) -> models.FenceDeclaration:
    fields = {
        "tools": tuple(CLAUDE_CLI_TOOLS.split(",")),
        "cwd_only": True,
        "strict_mcp": True,
        "no_prompts": True,
        "restricted": True,
        "permission_mode": CLAUDE_CLI_PERMISSION_MODE,
        "dropped_env": tuple(_DROP_ENV),
    }
    fields.update(changes)
    return models.FenceDeclaration(**fields)


class FakeConnector:
    """routes + run_session + fences + record; `results` are handed out one per session."""

    def __init__(self, choices, results, fences=None):
        self.choices = list(choices)
        self.results = list(results)
        self.declared = {"claude": claude_fence()} if fences is None else fences
        self.sessions: list[tuple[str, models.SessionRequest]] = []
        self.recorded: list[tuple[str, str, models.ModelChoice, models.Outcome]] = []
        self.route_calls: list[tuple[str, str, str]] = []

    def routes(self, role, *, task_id, substrate):
        self.route_calls.append((role, task_id, substrate))
        return list(self.choices)

    def fences(self):
        return self.declared

    def run_session(self, role, request):
        self.sessions.append((role, request))
        (request.cwd / "context" / "edited.txt").write_text(f"by {len(self.sessions)}")
        return self.results.pop(0)

    def record(self, role, task_id, choice, outcome):
        self.recorded.append((role, task_id, choice, outcome))


class NoSessionConnector:
    def __init__(self, choices):
        self.choices = choices

    def routes(self, role, *, task_id, substrate):
        return list(self.choices)


OK = models.SessionResult(text="answer", ok=True, cost_usd=0.5)
GONE = models.SessionResult(text="", ok=False, unavailable=True, error="rate limited")
BROKEN = models.SessionResult(text="", ok=False, error="boom", cost_usd=0.25)
A = models.ModelChoice("claude", "model-a", "high")
B = models.ModelChoice("claude", "model-b")


class ImproverConnectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.context = self.root / "ctx"
        self.context.mkdir()
        (self.context / "seed.txt").write_text("seed")
        self.audit = self.root / "audit"
        patch = mock.patch("harness2.core.proposer.time.sleep")
        patch.start()
        self.addCleanup(patch.stop)

    def proposer(self, connector, task="t1") -> Proposer:
        session = ModelSession(connector, "fake").for_task(task)
        return Proposer(CONNECTOR_SPEC, audit_dir=self.audit, models=session)

    def call(self, prop, mission="do it"):
        return prop.call(mission, self.context, "improve", harvest="context")

    def test_session_runs_in_the_connector_and_the_tree_comes_back(self):
        conn = FakeConnector([A], [OK])
        reply = self.call(self.proposer(conn))
        self.assertTrue(reply.ok)
        self.assertEqual(reply.text, "answer")
        self.assertEqual(reply.cost_usd, 0.5)
        self.assertEqual(len(conn.sessions), 1)
        role, request = conn.sessions[0]
        self.assertEqual(role, models.Role.IMPROVER)
        self.assertEqual(request.label, "improve")
        self.assertEqual(request.mission, "do it")
        self.assertTrue(request.cwd.name.startswith("harness2-prop-"))
        self.assertFalse(request.cwd.exists())  # the working directory is removed afterwards
        self.assertEqual((reply.workspace / "edited.txt").read_text(), "by 1")
        self.assertEqual((reply.workspace / "seed.txt").read_text(), "seed")
        self.assertEqual((reply.runner, reply.model, reply.effort), ("claude", "model-a", "high"))
        self.assertEqual(conn.route_calls, [(models.Role.IMPROVER, "t1", "")])

    def test_audit_meta_names_the_candidate(self):
        reply = self.call(self.proposer(FakeConnector([A], [OK])))
        meta = json.loads(
            reply.audit.with_name(
                reply.audit.name.replace(".response.md", ".meta.json")
            ).read_text()
        )
        self.assertEqual(meta["channel"], "connector")
        self.assertEqual(meta["runner"], "claude")
        self.assertEqual(meta["model"], "model-a")
        self.assertEqual(meta["effort"], "high")

    def test_unavailable_goes_to_the_next_candidate(self):
        conn = FakeConnector([A, B], [GONE, OK])
        prop = self.proposer(conn)
        reply = self.call(prop)
        self.assertEqual((reply.model, reply.cost_usd), ("model-b", 0.5))
        self.assertEqual([s[0] for s in conn.sessions], [models.Role.IMPROVER] * 2)
        rows = prop.models.records_for("t1")
        self.assertEqual(
            [(r["model"], r["n"], r["ok"], r["unavailable"]) for r in rows],
            [("model-a", 1, False, True), ("model-b", 1, True, False)],
        )
        self.assertEqual(len(conn.recorded), 2)
        self.assertEqual([c[1] for c in conn.recorded], ["t1", "t1"])
        self.assertEqual([c[2] for c in conn.recorded], [A, B])
        self.assertTrue(conn.recorded[0][3].unavailable)
        self.assertEqual(conn.recorded[1][3].label, "improve")

    def test_unfenced_runner_is_skipped_and_noted(self):
        other = models.ModelChoice("opencode", "prov/x")
        conn = FakeConnector(
            [other, A], [OK], fences={"claude": claude_fence(), "opencode": claude_fence()}
        )
        prop = self.proposer(conn)
        reply = self.call(prop)
        self.assertEqual(reply.model, "model-a")
        self.assertEqual(len(conn.sessions), 1)
        rows = prop.models.records_for("t1")
        self.assertEqual(rows[0]["runner"], "opencode")
        self.assertEqual(rows[0]["skipped_reason"], "no fence")
        self.assertEqual(rows[0]["n"], 0)
        self.assertEqual(len(conn.recorded), 1)

    def test_loose_declaration_is_not_called(self):
        loose = claude_fence(tools=(*CLAUDE_CLI_TOOLS.split(","), "Bash"))
        conn = FakeConnector([A], [OK], fences={"claude": loose})
        prop = self.proposer(conn)
        with self.assertRaises(ProposerError) as caught:
            self.call(prop)
        self.assertIn("no fenced improver model", str(caught.exception))
        self.assertEqual(conn.sessions, [])
        self.assertEqual(prop.models.records_for("t1")[0]["skipped_reason"], "no fence")

    def test_no_usable_candidate_fails_after_the_retries(self):
        conn = FakeConnector([A], [GONE, GONE])
        with self.assertRaises(ProposerError) as caught:
            self.call(self.proposer(conn))
        self.assertIn("no fenced improver model", str(caught.exception))
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(len(conn.sessions), 2)

    def test_other_failure_is_the_attempts_failure_and_is_retried(self):
        conn = FakeConnector([A, B], [BROKEN, OK])
        reply = self.call(self.proposer(conn))
        self.assertEqual(reply.attempts, 2)
        self.assertEqual(reply.model, "model-a")  # the second attempt starts from the first again
        self.assertEqual(reply.cost_usd, 0.75)
        self.assertEqual(len(conn.sessions), 2)

    def test_same_instruction_comes_from_the_cache(self):
        conn = FakeConnector([A], [OK])
        prop = self.proposer(conn)
        first = self.call(prop)
        second = self.call(prop)
        self.assertTrue(second.cached)
        self.assertEqual(second.text, first.text)
        self.assertEqual(len(conn.sessions), 1)

    def test_connector_channel_needs_a_model_session(self):
        with self.assertRaises(ValueError):
            Proposer(CONNECTOR_SPEC)
        with self.assertRaises(ValueError):
            Proposer(ChannelSpec("opencode", ""))

    def test_channel_spec_and_resolve(self):
        self.assertEqual(ChannelSpec("connector", "").model, "")
        with self.assertRaises(ValueError):
            resolve_channel_model("connector", "x")

    def test_for_task_separates_the_rows(self):
        conn = FakeConnector([A], [OK, OK])
        prop = self.proposer(conn, "t1")
        self.call(prop)
        _set_improver_task("t2", prop, prop)
        self.call(prop, mission="another")
        self.assertEqual(prop.models.records_for("t1")[0]["n"], 1)
        self.assertEqual(prop.models.records_for("t2")[0]["n"], 1)
        self.assertEqual([c[1] for c in conn.recorded], ["t1", "t2"])

    def test_cli_channel_for_a_connector_with_sessions(self):
        conn = FakeConnector([A], [])
        args = argparse.Namespace(timeout=77)
        channel, session = intake_cli._improver_channel(
            argparse.ArgumentParser(), args, conn, "fake"
        )
        self.assertEqual((channel.kind, channel.model, channel.timeout), ("connector", "", 77))
        self.assertIsInstance(session, ModelSession)

    def test_cli_channel_without_run_session_uses_the_first_candidate(self):
        conn = NoSessionConnector([models.ModelChoice("opencode", "prov/m1"), B])
        args = argparse.Namespace(timeout=60)
        channel, session = intake_cli._improver_channel(
            argparse.ArgumentParser(), args, conn, "fake"
        )
        self.assertIsNone(session)
        self.assertEqual(
            (channel.kind, channel.model, channel.timeout), ("opencode", "prov/m1", 60)
        )

    def test_runner_mismatch_fails_the_attempt(self):
        wrong = models.SessionResult(text="answer", ok=True, cost_usd=0.5, runner="opencode")
        conn = FakeConnector([A], [wrong])
        prop = self.proposer(conn)
        with self.assertRaises(ProposerError) as caught:
            prop.call("do it", self.context, "improve", max_attempts=1)
        self.assertIn("connector ran opencode instead of claude", str(caught.exception))
        (row,) = prop.models.records_for("t1")
        self.assertFalse(row["ok"])
        self.assertFalse(row["unavailable"])
        self.assertEqual(conn.recorded[0][3].error, "connector ran opencode instead of claude")

    def test_session_directory_is_under_session_root(self):
        conn = FakeConnector([A], [OK])
        root = self.root / "sessions"
        session = ModelSession(conn, "fake", session_root=root).for_task("t1")
        self.call(Proposer(CONNECTOR_SPEC, audit_dir=self.audit, models=session))
        cwd = conn.sessions[0][1].cwd
        self.assertEqual(cwd.parent, root)
        self.assertTrue(cwd.name.startswith("harness2-prop-"))

    def test_cli_session_root_default_and_option(self):
        conn = FakeConnector([A], [])
        stderr = io.StringIO()
        args = argparse.Namespace(timeout=60, session_root=self.root / "sr")
        with contextlib.redirect_stderr(stderr):
            _, session = intake_cli._improver_channel(argparse.ArgumentParser(), args, conn, "fake")
        self.assertEqual(session.session_root, self.root / "sr")
        self.assertIn(str(self.root / "sr"), stderr.getvalue())
        parser = intake_cli._build_parser()
        self.assertIsNone(parser.parse_args(["--repo", "r", "--work-root", "w"]).session_root)
        self.assertEqual(
            parser.parse_args(
                ["--repo", "r", "--work-root", "w", "--session-root", "d"]
            ).session_root,
            "d",
        )

    def test_cli_rejects_a_connector_with_no_fenced_runner(self):
        short = claude_fence(dropped_env=())
        conn = FakeConnector([A], [], fences={"claude": short})
        args = argparse.Namespace(timeout=60)
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(stderr):
            intake_cli._improver_channel(argparse.ArgumentParser(), args, conn, "fake")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("dropped_env", stderr.getvalue())

    def test_unavailable_cost_is_kept_when_the_next_candidate_raises(self):
        gone = models.SessionResult(text="", ok=False, unavailable=True, cost_usd=0.5)
        conn = FakeConnector([A, B], [gone])
        conn.results.append(RuntimeError("crash"))

        def run_session(role, request):
            result = conn.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        conn.run_session = run_session
        with self.assertRaises(ProposerError) as caught:
            self.proposer(conn).call("do it", self.context, "improve", max_attempts=1)
        self.assertEqual(caught.exception.cost_usd, 0.5)

    def test_empty_reply_is_recorded_as_a_failure(self):
        empty = models.SessionResult(text="  ", ok=True)
        conn = FakeConnector([A], [empty])
        prop = self.proposer(conn)
        with self.assertRaises(ProposerError):
            prop.call("do it", self.context, "improve", max_attempts=1)
        (row,) = prop.models.records_for("t1")
        self.assertFalse(row["ok"])
        self.assertEqual(conn.recorded[0][3].error, "empty reply")
        self.assertFalse(conn.recorded[0][3].ok)

    def test_cli_maps_the_claude_runner_to_the_claude_cli_channel(self):
        conn = NoSessionConnector([models.ModelChoice("claude", "m")])
        args = argparse.Namespace(timeout=60)
        channel, session = intake_cli._improver_channel(
            argparse.ArgumentParser(), args, conn, "fake"
        )
        self.assertIsNone(session)
        self.assertEqual((channel.kind, channel.model), ("claude_cli", "m"))

    def test_cli_channel_rejects_an_unknown_runner(self):
        conn = NoSessionConnector([models.ModelChoice("codex", "m")])
        args = argparse.Namespace(timeout=60)
        with self.assertRaises(SystemExit) as caught, mock.patch("sys.stderr"):
            intake_cli._improver_channel(argparse.ArgumentParser(), args, conn, "fake")
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
