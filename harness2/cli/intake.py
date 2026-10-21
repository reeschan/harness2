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

"""Take the tasks a task connector offers, run them, and print one report per task.

The connector is chosen with --connector: a built-in name, the name of an installed entry
point of the group harness2.task_connectors, or a `module:Attr` import path of a factory.
Each --connector-opt KEY=VALUE becomes a keyword argument of the factory. The CLI also offers
the keywords repo, mode, timeout and locate (a function returning where the run's summary
lies); a factory receives those of them its signature names (all of them when it takes
**kwargs), and a --connector-opt of the same name wins.

Each task runs with OpenCode in a git worktree of --repo (run_intake). For every task this
run delivered or failed, the connector's report(task_id) text is printed when it has one,
else the line `<task_id>: <status>`; the reports are taken before the connector is closed, and
a report that raises falls back to that line with an `error:` line on stderr. The exit status
is 0 when every task is DONE, 1 when any is not, the run failed or a report or close raised,
and 2 for an argument error.

    python3 -m harness2.cli.intake --connector mypkg.tasks:make_connector \\
        --connector-opt url=http://127.0.0.1:8000 --repo /home/me/proj --work-root /home/me/h2
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import inspect
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .. import config
from ..benchmarks.intake import IntakeAdapter, run_intake
from ..benchmarks.intake_worktree import OpencodeAgent, WorktreeLauncher
from ..core import intake, intake_report, normalize
from ..core.proposer import Proposer
from ..core.spec import resolve_channel_model
from ..core.worktree import WorktreeError, git_env
from . import _common as C


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harness2.cli.intake", description=__doc__, formatter_class=C.HelpFormatter
    )
    p.add_argument(
        "--connector",
        default=None,
        help=f"task connector name or module:Attr; default ${intake.ENV_VAR} or {intake.DEFAULT}",
    )
    p.add_argument(
        "--connector-opt",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="keyword argument of the connector factory (repeatable)",
    )
    p.add_argument(
        "--repo", required=True, help="absolute path of the root of the git work tree to change"
    )
    p.add_argument(
        "--work-root", required=True, help="the intake adapter's root; outside the --repo work tree"
    )
    p.add_argument("--runs-root", default=None, help="scheduler state; default <work-root>/runs")
    p.add_argument("--k", type=int, default=1, help="scale: R=4k rollouts per task")
    p.add_argument("--modes", default="agg", help=C.MODES_HELP)
    p.add_argument("--seed", type=int, default=None, help="enters the namespace")
    p.add_argument("--tag", default="", help="enters the namespace")
    p.add_argument(
        "--model", default=None, help="OpenCode model of the agent; default from the config"
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="seconds offered to the connector as `timeout`, and the channel's timeout",
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
            env=git_env(),
        )
    except OSError:
        return None
    top = proc.stdout.strip()
    if proc.returncode != 0 or not top:
        return None
    return Path(top).resolve()


def _parse_opts(parser: argparse.ArgumentParser, raw: list[str]) -> dict[str, str]:
    opts: dict[str, str] = {}
    for item in raw:
        key, sep, value = item.partition("=")
        if not sep or not key:
            parser.error(f"--connector-opt must be KEY=VALUE, got {item!r}")
        opts[key] = value
    return opts


def _check_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Argument errors exit with status 2 through parser.error, before any task is claimed."""
    args.opts = _parse_opts(parser, args.connector_opt)
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
    try:
        args.mode_tokens = C.parse_modes(args.modes)
    except ValueError as exc:
        parser.error(str(exc))


def _factory_kwargs(
    factory: Callable[..., Any], context: Mapping[str, Any], opts: Mapping[str, str]
) -> dict[str, Any]:
    """The context keywords the factory's signature names (all with **kwargs), then opts."""
    try:
        params = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        params = None
    if params is None or any(p.kind is p.VAR_KEYWORD for p in params):
        chosen = dict(context)
    else:
        named = {p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
        chosen = {k: v for k, v in context.items() if k in named}
    chosen.update(opts)
    return chosen


def _build_connector(
    parser: argparse.ArgumentParser, args: argparse.Namespace, context: Mapping[str, Any]
) -> intake.TaskConnector:
    """The connector named by --connector; an unknown name or a bad result exits with 2."""
    name = intake.connector_name(name=args.connector)
    try:
        factory = intake.resolve_factory(name)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    try:
        conn = factory(**_factory_kwargs(factory, context, args.opts))
    except (TypeError, ValueError) as exc:
        # A keyword the factory does not take or a missing required setting: an argument error.
        parser.error(f"task connector {name!r}: {exc}")
    if not isinstance(conn, intake.TaskConnector):
        parser.error(
            f"task connector {name!r} factory returned {type(conn).__name__}, "
            f"which is not a TaskConnector"
        )
    return conn


class _Recorder:
    """A TaskConnector that records this run's status of each task, then hands on the call."""

    def __init__(self, inner: intake.TaskConnector, mode: str):
        self._inner = inner
        self._mode = mode
        self.statuses: dict[str, str] = {}

    def poll(self):
        return self._inner.poll()

    def fetch_files(self, raw, dest):
        return self._inner.fetch_files(raw, dest)

    def deliver(self, task_id, deliverable_dir, summary):
        # Recorded only once the real deliver returned; when it raises, the caller's fail records.
        result = self._inner.deliver(task_id, deliverable_dir, summary)
        self.statuses[task_id] = intake_report.report_status(summary, mode=self._mode)
        return result

    def fail(self, task_id, reason):
        # BLOCKED even when the real fail raises: the task was not delivered either way.
        try:
            return self._inner.fail(task_id, reason)
        finally:
            self.statuses[task_id] = intake_report.BLOCKED

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def main(argv: list[str] | None = None) -> int:
    """Parse, then run `_main`, printing a run-time failure as one `error:` line."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    _check_args(parser, args)
    try:
        return _main(parser, args)
    except (
        config.ConfigError,
        WorktreeError,
        RuntimeError,
        ValueError,
        FileNotFoundError,
    ) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _main(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    work_root = Path(args.work_root).expanduser().resolve()
    runs_root = Path(args.runs_root) if args.runs_root else work_root / "runs"
    modes = args.mode_tokens
    model = args.model or config.SOLVER_MODEL_OC

    # The connector is built before the adapter whose names locate needs, so it forwards.
    target = None

    def locate(summary):
        return (
            target(summary) if target is not None else {"summary_json": None, "harness_dir": None}
        )

    context = {"repo": str(repo), "mode": modes[0], "timeout": args.timeout, "locate": locate}
    connector = _build_connector(parser, args, context)
    try:
        caps = intake.capabilities(connector)
    except Exception as exc:
        # No caps yet, so close the connector directly; a failing close does not hide `exc`.
        close = getattr(connector, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
        parser.error(str(exc))
    recorder = _Recorder(connector, modes[0])

    error: Exception | None = None
    launcher = None
    blocks: list[str] = []
    errors: list[str] = []
    close_error: Exception | None = None
    try:
        try:
            launcher = WorktreeLauncher(
                repo,
                OpencodeAgent(model),
                exclude=caps.workspace_policy.exclude,
                scrub_env=caps.scrub_env,
            )
            channel = resolve_channel_model(None, None, effort="high", timeout=args.timeout)
            adapter = IntakeAdapter(
                work_root,
                recorder,
                normalize.normalizer_for(),
                launcher=launcher,
                channel=channel,
                judge=caps.judge,
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
            run_intake(
                adapter,
                prop,
                k=args.k,
                runs_root=runs_root,
                modes=modes,
                seed=args.seed,
                tag=args.tag,
            )
        except Exception as exc:
            error = exc
        finally:
            # Also on a BaseException; a failing cleanup does not hide the run's own error.
            if launcher is not None:
                try:
                    launcher.cleanup()
                except Exception as exc:
                    if error is None:
                        error = exc
        # Before close: a connector may need what close lets go of to build its reports.
        blocks, errors = _reports(caps, recorder.statuses)
    finally:
        try:
            caps.close()
        except Exception as exc:
            close_error = exc

    if blocks:
        print("\n\n".join(blocks))
    else:
        print("no task was taken up", file=sys.stderr)
    if error is not None:
        errors.append(f"error: {error}")
    if close_error is not None:
        errors.append(f"error: close: {close_error}")
    for line in errors:
        print(line, file=sys.stderr)
    if errors:
        return 1
    return 0 if all(s == intake_report.DONE for s in recorder.statuses.values()) else 1


def _reports(caps: intake.Capabilities, statuses: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """One block per task (its report, else `<task_id>: <status>`) and an `error:` line for
    each report that raised; a raising report falls back to the status line."""
    blocks: list[str] = []
    errors: list[str] = []
    for task_id, status in statuses.items():
        text = None
        if caps.report is not None:
            try:
                text = caps.report(task_id)
            except Exception as exc:
                errors.append(f"error: report {task_id}: {exc}")
        blocks.append(text if text is not None else f"{task_id}: {status}")
    return blocks, errors


if __name__ == "__main__":
    sys.exit(main())
