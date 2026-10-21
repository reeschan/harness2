"""The claude_cli improver session is fenced to its working directory (no command tools)."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness2 import config
from harness2.core import proposer
from harness2.core.spec import ChannelSpec

FENCE_TOOLS = "Read,Edit,Write,Glob,Grep"


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _value(argv: list[str], flag: str) -> str:
    i = argv.index(flag)
    return argv[i + 1]


class ClaudeCliFenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)

    def _run_claude(self, system: str = "") -> mock.MagicMock:
        p = proposer.Proposer(
            spec=ChannelSpec(kind="claude_cli", model="model-q"), project="proj-x", region="r-1"
        )
        out = json.dumps({"result": "done", "subtype": "success", "total_cost_usd": 0.5})
        with mock.patch.object(proposer.subprocess, "run", return_value=_completed(out)) as run:
            text, cost, err = p._claude_cli(self.cwd, "mission", system, 30)
        self.assertEqual((text, cost, err), ("done", 0.5, ""))
        run.assert_called_once()
        return run

    def test_argv_drops_skip_permissions(self) -> None:
        argv = self._run_claude().call_args.args[0]
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--allow-dangerously-skip-permissions", argv)

    def test_argv_has_fence_flags(self) -> None:
        argv = self._run_claude(system="be brief").call_args.args[0]
        self.assertIn("--restricted", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(_value(argv, "--permission-mode"), "acceptEdits")
        self.assertEqual(_value(argv, "--permission-prompts"), "none")
        self.assertEqual(_value(argv, "--tools"), FENCE_TOOLS)
        self.assertNotIn("Bash", _value(argv, "--tools"))
        # the existing non-interactive JSON flags are still there
        self.assertIn("-p", argv)
        self.assertEqual(_value(argv, "--output-format"), "json")
        self.assertEqual(_value(argv, "--effort"), "high")
        self.assertEqual(_value(argv, "--append-system-prompt"), "be brief")

    def test_runs_in_cwd_with_vertex_env(self) -> None:
        kw = self._run_claude().call_args.kwargs
        self.assertEqual(kw["cwd"], self.cwd)
        self.assertEqual(kw["input"], "mission")
        env = kw["env"]
        self.assertEqual(env["CLAUDE_CODE_USE_VERTEX"], "1")
        self.assertEqual(env["ANTHROPIC_VERTEX_PROJECT_ID"], "proj-x")
        self.assertEqual(env["CLOUD_ML_REGION"], "r-1")
        self.assertEqual(env["ANTHROPIC_MODEL"], "model-q")

    def test_context_is_copied_under_cwd(self) -> None:
        """call() copies the context under the session cwd, so no --add-dir is needed."""
        ctx = Path(self.tmp.name) / "ctx"
        ctx.mkdir()
        (ctx / "f.txt").write_text("x", encoding="utf-8")
        seen: dict = {}

        def fake(cwd, mission, system, wall):
            seen["cwd"] = cwd
            seen["has_ctx"] = (cwd / "context" / "f.txt").is_file()
            return "ok", 0.0, ""

        p = proposer.Proposer(spec=ChannelSpec(kind="claude_cli", model="model-q"))
        with (
            mock.patch.object(proposer.Proposer, "_claude_cli", side_effect=fake, autospec=False),
            mock.patch.object(proposer.Proposer, "_cached", return_value=None),
            mock.patch.object(proposer.Proposer, "_write_audit"),
        ):
            p.call("m", ctx, "lbl")
        self.assertTrue(seen["has_ctx"])


class OpencodeUnchangedTest(unittest.TestCase):
    def test_opencode_argv_and_permission(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d)
            oc = Path("/fake/opencode")
            p = proposer.Proposer(
                spec=ChannelSpec(kind="opencode", model="prov/model-a"),
                project="proj-x",
                region="r-1",
            )
            with (
                mock.patch.object(proposer, "_opencode_dir", return_value=oc),
                mock.patch.object(proposer.subprocess, "run", return_value=_completed("")) as run,
            ):
                p._opencode(cwd, "mission", "", 30)
            argv = run.call_args.args[0]
            kw = run.call_args.kwargs
            self.assertEqual(
                argv,
                [
                    str(config.BUN),
                    "run",
                    "--cwd",
                    str(oc / "packages" / "opencode"),
                    "--conditions=browser",
                    "src/index.ts",
                    "run",
                    "Read MISSION.md first. Do what it asks. Reply with the requested output as "
                    "your final message AND save it verbatim to output/answer.md.",
                    "--dir",
                    str(cwd),
                    "--model",
                    "prov/model-a",
                    "--format",
                    "json",
                    "--variant",
                    "high",
                ],
            )
            self.assertEqual(kw["cwd"], cwd)
            self.assertEqual(
                json.loads(kw["env"]["OPENCODE_PERMISSION"]),
                {"external_directory": {"*": "deny", str(cwd): "allow", f"{cwd}/**": "allow"}},
            )
            self.assertEqual(kw["env"]["GOOGLE_VERTEX_PROJECT"], "proj-x")


if __name__ == "__main__":
    unittest.main()
