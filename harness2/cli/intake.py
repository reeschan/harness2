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

"""Take the queued PBIs of one repository from jev-mcp, run them, and print their reports.

Claims every queued task through jev-mcp, runs each one with OpenCode in a git worktree of
--repo (run_intake), applies the chosen run's diff back to --repo, and prints the five-line
report jev-mcp wrote for every task taken up in this run.

Run jev-mcp and the harness with the same view of the file system (on Windows, both in
WSL), and put --work-root under jev-mcp's --root: verify's workdir must lie under --root.

    python3 -m harness2.cli.intake --repo /home/me/proj --work-root /home/me/jev/h2 \\
        --mcp-url http://127.0.0.1:7830/mcp
"""

from __future__ import annotations

import argparse
import functools
import shlex
import subprocess
import sys
from pathlib import Path

from .. import config
from ..benchmarks.intake import HANDLED_MARK, IntakeAdapter, run_intake
from ..benchmarks.intake_worktree import OpencodeAgent, WorktreeLauncher
from ..core import intake, intake_report, normalize
from ..core.intake_mcp import McpVerifyJudge
from ..core.mcp_client import McpError
from ..core.proposer import Proposer
from ..core.spec import resolve_channel_model
from ..core.worktree import WorktreeError
from . import _common as C

CONNECTOR = "jev-mcp"
REPORT_DIR = Path("docs") / "pbi" / "mcp"
STATUS_PREFIX = "状態: "


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harness2.cli.intake", description=__doc__, formatter_class=C.HelpFormatter
    )
    p.add_argument("--repo", required=True, help="absolute path of the repository the PBIs change")
    server = p.add_mutually_exclusive_group(required=True)
    server.add_argument("--mcp-url", default=None, help="jev-mcp over HTTP, e.g. a /mcp URL")
    server.add_argument(
        "--mcp-command", default=None, help="command that starts jev-mcp on stdio (shlex-split)"
    )
    p.add_argument(
        "--work-root",
        required=True,
        help="the intake adapter's root; under jev-mcp's --root, outside --repo",
    )
    p.add_argument("--runs-root", default=None, help="scheduler state; default <work-root>/runs")
    p.add_argument("--k", type=int, default=1, help="scale: R=4k rollouts per task")
    p.add_argument("--modes", default="agg", help=C.MODES_HELP)
    p.add_argument("--seed", type=int, default=None, help="enters the namespace")
    p.add_argument("--tag", default="", help="enters the namespace")
    p.add_argument(
        "--model", default=None, help="OpenCode model of the agent; default config.SOLVER_MODEL_OC"
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="seconds for one jev-mcp call (verify runs the PBI's commands) and the channel",
    )
    return p


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _toplevel(repo: Path) -> Path | None:
    """The root of the git work tree `repo` is in, or None when it is not in one."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    top = proc.stdout.strip()
    if proc.returncode != 0 or not top:
        return None
    return Path(top).resolve()


def _check_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Argument errors exit with status 2 through parser.error, before any task is claimed."""
    repo = Path(args.repo)
    if not repo.is_absolute():
        parser.error(f"--repo must be an absolute path, got {args.repo!r}")
    if not repo.is_dir():
        parser.error(f"--repo is not a directory: {args.repo}")
    top = _toplevel(repo)
    if top is None:
        parser.error(f"--repo is not in a git work tree: {args.repo}")
    work_root = Path(args.work_root).expanduser().resolve()
    if _inside(work_root, top):
        parser.error(f"--work-root {work_root} is inside the --repo work tree {top}")
    if repo.resolve() != top:
        parser.error(f"--repo must be the root of its git work tree ({top}), got {args.repo}")
    if args.k < 1:
        parser.error("--k must be positive")
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if args.mcp_command is not None and not shlex.split(args.mcp_command):
        parser.error("--mcp-command is empty")
    try:
        args.mode_tokens = C.parse_modes(args.modes)
    except ValueError as exc:
        parser.error(str(exc))


def _handled(adapter: IntakeAdapter) -> set[str]:
    return {t for t in adapter.tasks() if (adapter.root / "tasks" / t / HANDLED_MARK).is_file()}


def _report(repo: Path, task_id: str) -> tuple[str, str | None]:
    """The report text to print and its status (None when no report was written)."""
    path = repo / REPORT_DIR / f"{task_id}.last.md"
    if not path.is_file():
        return f"{task_id}: no report written", None
    text = path.read_text(encoding="utf-8").strip("\n")
    first = text.splitlines()[0] if text else ""
    status = first[len(STATUS_PREFIX) :].strip() if first.startswith(STATUS_PREFIX) else None
    return text, status


def main(argv: list[str] | None = None) -> int:
    """Parse, then run `_main`, printing a run-time failure as one `error:` line."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    _check_args(parser, args)
    try:
        return _main(args)
    except (
        config.ConfigError,
        McpError,
        WorktreeError,
        RuntimeError,
        ValueError,
        FileNotFoundError,
    ) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _main(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    work_root = Path(args.work_root).expanduser().resolve()
    runs_root = Path(args.runs_root) if args.runs_root else work_root / "runs"
    modes = args.mode_tokens
    model = args.model or config.SOLVER_MODEL_OC
    command = shlex.split(args.mcp_command) if args.mcp_command is not None else None

    # The connector is built before the adapter whose names locate needs, so it forwards.
    target = None

    def locate(summary):
        return (
            target(summary) if target is not None else {"summary_json": None, "harness_dir": None}
        )

    connector = intake.connector_for(
        name=CONNECTOR,
        repo=str(repo),
        url=args.mcp_url,
        command=command,
        mode=modes[0],
        locate=locate,
        timeout=args.timeout,
    )
    client = connector.client
    launcher = None
    try:
        launcher = WorktreeLauncher(repo, OpencodeAgent(model))
        channel = resolve_channel_model(None, None, effort="high", timeout=args.timeout)
        adapter = IntakeAdapter(
            work_root,
            connector,
            normalize.normalizer_for(),
            launcher=launcher,
            channel=channel,
            judge=McpVerifyJudge(client, repo),
        )
        target = functools.partial(
            intake_report.summary_locations,
            runs_root,
            bench=adapter.spec.name,
            substrate=adapter.substrate.name,
            mode=modes[0],
            tag=args.tag,
        )
        summary_ns = C.summary_namespace(adapter, args.k, seed=args.seed, tag=args.tag)
        prop = Proposer(
            adapter.spec.channels["proposer"], audit_dir=runs_root / "_audit" / summary_ns
        )
        before = _handled(adapter)
        run_intake(
            adapter,
            prop,
            k=args.k,
            runs_root=runs_root,
            modes=modes,
            seed=args.seed,
            tag=args.tag,
        )
        taken = sorted(_handled(adapter) - before)
    finally:
        try:
            if launcher is not None:
                launcher.cleanup()
        finally:
            client.close()

    blocks: list[str] = []
    statuses: list[str | None] = []
    for task_id in taken:
        text, status = _report(repo, task_id)
        blocks.append(text)
        statuses.append(status)
    if blocks:
        print("\n\n".join(blocks))
    else:
        print("no task was taken up", file=sys.stderr)
    return 0 if all(s == intake_report.DONE for s in statuses) else 1


if __name__ == "__main__":
    sys.exit(main())
