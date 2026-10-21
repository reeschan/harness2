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

"""core.intake_mcp with a fake MCP client and real git repositories under a temporary dir."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness2.benchmarks.intake import IntakeAdapter, run_intake
from harness2.core import intake_report
from harness2.core import worktree as wt
from harness2.core.intake import TaskConnector, connector_for, validate_task_spec
from harness2.core.intake_mcp import (
    DOMAIN,
    RUNNER,
    McpTaskConnector,
    McpVerifyJudge,
)
from harness2.core.mcp_client import HttpMcpClient, McpError, McpToolError
from harness2.core.normalize import PassthroughNormalizer, normalize_task
from harness2.core.proposer import Proposer, Reply
from harness2.core.spec import ChannelSpec
from harness2.core.worktree import WorktreeError

REAL_UNCHANGED = Path(__file__).resolve().parents[1] / "docs" / "pbi" / "tools" / "unchanged.py"
CHANNEL = ChannelSpec("opencode", "fake-model")
REPORT = "PBI-007"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(cwd, *args):
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


class FakeClient:
    """Records every tool call; answers from `handlers` (a value, a callable or an exception)."""

    def __init__(self, handlers=None):
        self.calls: list[tuple[str, dict]] = []
        self.handlers = dict(handlers or {})
        self.closed = False

    def call_tool(self, name, arguments):
        args = dict(arguments)
        self.calls.append((name, args))
        handler = self.handlers.get(name, {})
        if isinstance(handler, BaseException):
            raise handler
        if callable(handler):
            return handler(args)
        return handler

    def close(self):
        self.closed = True

    def names(self):
        return [name for name, _ in self.calls]

    def args_of(self, name):
        return [args for n, args in self.calls if n == name]


def claim_for(report, *, rework=None, worker_md=None):
    return {
        "repo": "/repo",
        "report": report,
        "pbi": report.split(".")[0],
        "prompt": f"Implement {report}.",
        "pbi_markdown": f"# {report}: change a.txt\n\n## 受け入れ条件\n1. a.txt changes",
        "sections": {},
        "verify_commands": ["python3 -m unittest"],
        "rework": rework,
        "facts": None,
        "route": None,
        "worker_md": worker_md,
        "summary_path": "",
        "details_path": "",
    }


VERIFY_RESULTS = [
    {"n": 1, "command": "true", "code": 0},
    {"n": 2, "command": "false", "code": 1},
    {"n": 3, "command": "true", "code": 0},
]


class SourceRepoBase(unittest.TestCase):
    """A source git repository with the real unchanged.py and a record for PBI-007."""

    def setUp(self):
        env = mock.patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
        )
        env.start()
        self.addCleanup(env.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "src"
        self.src.mkdir()
        git(self.src, "init", "-q", "-b", "main")
        (self.src / "a.txt").write_text("alpha\n")
        (self.src / "keep.txt").write_text("keep\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "init")
        tools = self.src / "docs" / "pbi" / "tools"
        tools.mkdir(parents=True)
        shutil.copyfile(REAL_UNCHANGED, tools / "unchanged.py")
        record = self.src / "docs" / "pbi" / "verify" / f"{REPORT}.unchanged.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"keep.txt": sha(b"keep\n")}), encoding="utf-8")

    def make_run(self, run_dir, *, touch_keep=False):
        """A run like the worktree launcher leaves: a worktree with a.txt edited, and its diff."""
        worktree = run_dir / wt.WORKTREE_DIRNAME
        base = wt.prepare_worktree(self.src, worktree)
        (worktree / "a.txt").write_text("alpha by agent\n")
        if touch_keep:
            (worktree / "keep.txt").write_text("touched\n")
        out = run_dir / "workspace" / "output"
        out.mkdir(parents=True, exist_ok=True)
        wt.collect_diff(worktree, base, out / wt.DIFF_NAME)
        return out


class PollTest(unittest.TestCase):
    def client(self, *, claim_error_for=(), rework=None, worker_md=None):
        def claim(args):
            if args["report"] in claim_error_for:
                raise McpToolError("already claimed")
            return claim_for(args["report"], rework=rework, worker_md=worker_md)

        return FakeClient(
            {
                "list_tasks": {
                    "repo": "/repo",
                    "tasks": [
                        {"report": "PBI-001", "pbi": "PBI-001", "title": "one", "state": "queued"},
                        {"report": "PBI-002", "pbi": "PBI-002", "title": "two", "state": "claimed"},
                        {"report": "PBI-003", "pbi": "PBI-003", "title": "three", "state": "done"},
                        {
                            "report": "PBI-004.rework-1",
                            "pbi": "PBI-004",
                            "title": "four",
                            "state": "queued",
                        },
                    ],
                },
                "claim_task": claim,
            }
        )

    def test_claims_only_queued(self):
        client = self.client()
        raws = McpTaskConnector(client, "/repo").poll()
        self.assertIsInstance(raws, list)
        self.assertEqual([r.id for r in raws], ["PBI-001", "PBI-004.rework-1"])
        self.assertEqual(client.args_of("list_tasks"), [{"repo": "/repo"}])
        self.assertEqual(
            client.args_of("claim_task"),
            [
                {"repo": "/repo", "report": "PBI-001", "runner": RUNNER},
                {"repo": "/repo", "report": "PBI-004.rework-1", "runner": RUNNER},
            ],
        )
        self.assertEqual(raws[0].title, "one")
        self.assertEqual(raws[0].body, "Implement PBI-001.")
        self.assertEqual(raws[0].meta["pbi"], "PBI-001")
        self.assertEqual(raws[0].meta["verify_commands"], ["python3 -m unittest"])

    def test_claim_error_skips_that_task(self):
        client = self.client(claim_error_for=("PBI-001",))
        raws = McpTaskConnector(client, "/repo").poll()
        self.assertEqual([r.id for r in raws], ["PBI-004.rework-1"])
        self.assertEqual(len(client.args_of("claim_task")), 2)

    def test_task_spec_passes_and_normalizes(self):
        raws = McpTaskConnector(self.client(), "/repo").poll()
        for raw in raws:
            with self.subTest(raw=raw.id):
                self.assertEqual(validate_task_spec(raw.meta["task_spec"]), [])
                spec = normalize_task(PassthroughNormalizer(), raw, domains=())
                self.assertEqual(spec.task_id, raw.id)
                self.assertEqual(spec.domain, DOMAIN)
                self.assertEqual(spec.files, ())
                self.assertEqual(spec.deliverable_where, "changes.diff")
                self.assertEqual(spec.prompts.domain_word, "PBI")
                self.assertEqual(spec.private_criteria, ())
                self.assertIn("Implement", spec.prompt)
                self.assertIn("受け入れ条件", spec.prompt)

    def test_rework_and_worker_md_reach_the_prompt(self):
        client = self.client(rework="Fix the edge case.", worker_md="Read the PBI first.")
        raw = McpTaskConnector(client, "/repo").poll()[0]
        prompt = raw.meta["task_spec"]["prompt"]
        self.assertIn("Fix the edge case.", prompt)
        self.assertIn("Read the PBI first.", prompt)
        plain = McpTaskConnector(self.client(), "/repo").poll()[0]
        self.assertNotIn("差し戻し", plain.meta["task_spec"]["prompt"])

    def test_fetch_files_makes_dest(self):
        dest = Path(tempfile.mkdtemp()) / "files"
        self.addCleanup(shutil.rmtree, dest.parent, True)
        McpTaskConnector(FakeClient(), "/repo").fetch_files(None, dest)
        self.assertTrue(dest.is_dir())
        self.assertEqual(list(dest.iterdir()), [])


class DeliverTest(SourceRepoBase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.tmp / "intake" / "executions" / "r1"
        self.seen_a = {}

        def verify(args):
            self.seen_a["verify"] = (self.src / "a.txt").read_text()
            return {"code": 1, "table": "", "stderr": "", "results": VERIFY_RESULTS}

        def submit(args):
            self.seen_a["submit"] = (self.src / "a.txt").read_text()
            return {"ok": True}

        self.client = FakeClient({"verify": verify, "submit_result": submit})
        self.conn = McpTaskConnector(self.client, self.src)

    def summary(self, *, errors=()):
        return {
            "task_id": REPORT,
            "domain": DOMAIN,
            "k": 1,
            "seed": None,
            "status": "ok",
            "base": [str(self.run_dir)],
            "modes": {},
            "selections": {"agg": {"chosen_run_dir": self.run_dir, "chosen_label": "base"}},
            "errors": list(errors),
        }

    def test_verify_apply_submit_in_order(self):
        output = self.make_run(self.run_dir)
        summary = self.summary()
        self.conn.deliver(REPORT, output, summary)
        self.assertEqual(self.client.names(), ["verify", "submit_result"])
        self.assertEqual(self.seen_a, {"verify": "alpha\n", "submit": "alpha by agent\n"})
        self.assertEqual((self.src / "a.txt").read_text(), "alpha by agent\n")
        (verify,) = self.client.args_of("verify")
        self.assertEqual(
            verify,
            {
                "repo": str(self.src),
                "report": REPORT,
                "workdir": str(self.run_dir / "worktree"),
                "name": f"{REPORT}.harness2",
            },
        )
        (submit,) = self.client.args_of("submit_result")
        self.assertEqual((submit["passed"], submit["total"]), (2, 3))
        self.assertEqual(submit["changed"], ["a.txt"])
        self.assertEqual(submit["status"], intake_report.report_status(summary, mode="agg"))
        self.assertEqual(submit["status"], "DONE")
        self.assertEqual(submit["unresolved"], "")
        self.assertIn("pass 2/3", submit["details"])
        json.dumps(submit["artifacts"])
        self.assertEqual(
            submit["artifacts"]["selections"]["agg"]["chosen_run_dir"], str(self.run_dir)
        )

    def test_no_results_counts_zero_and_errors_are_unresolved(self):
        self.client.handlers["verify"] = {"code": 2, "results": None}
        output = self.make_run(self.run_dir)
        self.conn.deliver(REPORT, output, self.summary(errors=["one", "x" * 3000]))
        (submit,) = self.client.args_of("submit_result")
        self.assertEqual((submit["passed"], submit["total"]), (0, 0))
        self.assertEqual(submit["status"], "DONE_WITH_CONCERNS")
        self.assertTrue(submit["unresolved"].startswith("one; x"))
        self.assertEqual(len(submit["unresolved"]), 2000)

    def test_locate_reaches_the_details(self):
        located = self.tmp / "runs" / "summary.json"
        conn = McpTaskConnector(
            self.client, self.src, locate=lambda s: {"summary_json": located, "harness_dir": None}
        )
        conn.deliver(REPORT, self.make_run(self.run_dir), self.summary())
        (submit,) = self.client.args_of("submit_result")
        self.assertIn(str(located), submit["details"])

    def test_failed_unchanged_check_raises_and_keeps_source(self):
        output = self.make_run(self.run_dir, touch_keep=True)
        with self.assertRaises(WorktreeError):
            self.conn.deliver(REPORT, output, self.summary())
        self.assertEqual(self.client.names(), ["verify"])
        self.assertEqual((self.src / "a.txt").read_text(), "alpha\n")
        self.assertEqual((self.src / "keep.txt").read_text(), "keep\n")

    def test_fail_sends_blocked(self):
        self.conn.fail(REPORT, "boom")
        self.conn.fail(REPORT, "")
        self.assertEqual(
            self.client.args_of("fail_task"),
            [
                {"repo": str(self.src), "report": REPORT, "status": "BLOCKED", "reason": "boom"},
                {"repo": str(self.src), "report": REPORT, "status": "BLOCKED", "reason": "failed"},
            ],
        )


class JudgeTest(unittest.TestCase):
    def test_frac(self):
        client = FakeClient({"verify": {"code": 1, "results": VERIFY_RESULTS}})
        judge = McpVerifyJudge(client, "/repo")
        verdict = judge(Path("/runs/r1"), REPORT, [])
        self.assertEqual(verdict["passed"], 2)
        self.assertEqual(verdict["total"], 3)
        self.assertAlmostEqual(verdict["frac"], 2 / 3)
        self.assertEqual(verdict["results"], VERIFY_RESULTS)
        (args,) = client.args_of("verify")
        self.assertEqual(args["workdir"], str(Path("/runs/r1") / "worktree"))
        self.assertEqual(args["name"], f"{REPORT}.judge")

    def test_empty_results_frac_zero(self):
        judge = McpVerifyJudge(FakeClient({"verify": {"code": 0, "results": []}}), "/repo")
        self.assertEqual(judge(Path("/r"), REPORT, [])["frac"], 0.0)

    def test_mcp_error_is_none(self):
        for exc in (McpError("down"), McpToolError("bad report")):
            with self.subTest(exc=type(exc).__name__):
                judge = McpVerifyJudge(FakeClient({"verify": exc}), "/repo")
                self.assertIsNone(judge(Path("/r"), REPORT, []))


class RegistryTest(unittest.TestCase):
    def test_connector_for_jev_mcp(self):
        with mock.patch.object(HttpMcpClient, "_send_request") as send:
            conn = connector_for(name="jev-mcp", repo="/repo", url="http://127.0.0.1:1/mcp")
        send.assert_not_called()
        self.assertIsInstance(conn, McpTaskConnector)
        self.assertIsInstance(conn, TaskConnector)
        self.assertIsInstance(conn.client, HttpMcpClient)
        self.assertEqual(conn.repo, "/repo")


class ScriptedProposer(Proposer):
    """Scripted replies like tests/test_intake_e2e.py's FakeImprover, without the records."""

    def call(self, mission, context_dir, label, **kwargs) -> Reply:
        context = Path(context_dir) if context_dir is not None else None
        workspace = None
        if label.startswith("select."):
            candidates = sorted((context / "candidates").glob("*.md"))
            text = f"<why>The first candidate.</why>\n```choice\n{candidates[0].stem}\n```"
        elif label.startswith("experience."):
            text = "NO_ENTRY"
        else:
            workspace = context / "harness"
            prompt = workspace / "systemprompt.md"
            prompt.write_text(
                prompt.read_text(encoding="utf-8") + "Run the tests first.\n", encoding="utf-8"
            )
            text = "<notes>Run the tests first.</notes>"
        reply = Reply(label=label, text=text, ok=True, attempts=1, workspace=workspace)
        self._calls.append(reply)
        return reply


class WorktreeLauncherStub:
    """Makes the run's worktree, edits a.txt there, and collects the diff into output/."""

    def __init__(self, source):
        self.source = source
        self.calls = 0

    def launch(self, *, workspace, prompt, install, run_dir, timeout):
        self.calls += 1
        worktree = run_dir / wt.WORKTREE_DIRNAME
        base = wt.prepare_worktree(self.source, worktree)
        (worktree / "a.txt").write_text("alpha by agent\n")
        wt.collect_diff(worktree, base, workspace / "output" / wt.DIFF_NAME)
        (run_dir / "trajectory.md").write_text(
            "[ACTION] edit a.txt\n[OBSERVATION]\ndone\n", encoding="utf-8"
        )


class RunIntakeTest(SourceRepoBase):
    def test_one_task_is_submitted_once(self):
        client = FakeClient(
            {
                "list_tasks": {
                    "repo": str(self.src),
                    "tasks": [
                        {"report": REPORT, "pbi": REPORT, "title": "seven", "state": "queued"}
                    ],
                },
                "claim_task": lambda args: claim_for(args["report"]),
                "verify": {"code": 0, "results": VERIFY_RESULTS[:1]},
                "submit_result": {"ok": True},
            }
        )
        launcher = WorktreeLauncherStub(self.src)
        adapter = IntakeAdapter(
            self.tmp / "intake",
            McpTaskConnector(client, self.src),
            PassthroughNormalizer(),
            launcher=launcher,
            channel=CHANNEL,
        )
        results = run_intake(
            adapter,
            ScriptedProposer(CHANNEL),
            k=1,
            runs_root=self.tmp / "runs",
            modes=("agg",),
        )
        self.assertEqual([r.task_id for r in results], [REPORT])
        self.assertGreater(launcher.calls, 0)
        self.assertEqual(client.names().count("submit_result"), 1)
        self.assertNotIn("fail_task", client.names())
        (submit,) = client.args_of("submit_result")
        self.assertEqual((submit["passed"], submit["total"]), (1, 1))
        self.assertEqual(submit["changed"], ["a.txt"])
        self.assertEqual((self.src / "a.txt").read_text(), "alpha by agent\n")
        self.assertEqual(
            (self.tmp / "intake" / "tasks" / REPORT / ".handled").read_text(encoding="utf-8"),
            "delivered",
        )


if __name__ == "__main__":
    unittest.main()
