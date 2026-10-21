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

"""A Launcher for code-change tasks: each rollout runs an agent in a git worktree.

`WorktreeLauncher.launch` makes `run_dir/worktree/` a worktree of the source repository
(`core.worktree.prepare_worktree`), runs the agent there, and writes the diff from the base
commit to `workspace/output/changes.diff` (nothing when the agent changed nothing). Paths in
`exclude` (relative to the source root) are left out of that diff, and the environment
variables named in `scrub_env` are removed from what the agent receives; both come from the
caller (the task connector's capabilities), the launcher names none of its own. The worktree
stays until `cleanup()`: the verification and `apply_back` read it later. Nothing goes back
to the source here.

`OpencodeAgent` is the real agent: OpenCode (the substrate's reference argv and env) run with
the worktree as its directory. The harness is carried in the prompt, never as files in the
worktree, so that it does not show up in the diff.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable

from ..core import worktree
from ..substrate.base import Install
from ..substrate.opencode import OpencodeSubstrate, compose_prompt

WORKTREE_PREAMBLE = (
    "Your working directory is a git worktree of the repository. Make your changes directly "
    "in it. The changes are collected automatically as a diff, so ignore any instruction to "
    "place files in output/. Do not commit."
)

_PLACEHOLDER = "{{ instruction }}"
EVENTS_NAME = "events.jsonl"
STDERR_NAME = "opencode.stderr"
TRAJECTORY_NAME = "trajectory.md"


@runtime_checkable
class AgentRunner(Protocol):
    """Runs an agent in `worktree` with `prompt`; writes the trajectory to
    `run_dir/trajectory.md`. Raises on failure or when `timeout` (seconds) runs out."""

    def run(
        self,
        *,
        worktree: Path,
        prompt: str,
        install: Install | None,
        run_dir: Path,
        timeout: int,
        env: Mapping[str, str],
    ) -> None: ...


def _prune(source: Path) -> None:
    """`git worktree prune` in `source`: forget worktrees whose directory is gone."""
    proc = subprocess.run(
        ["git", "-C", str(source), "worktree", "prune"],
        capture_output=True,
        text=True,
        check=False,
        env=worktree.git_env(),
    )
    if proc.returncode != 0:
        raise worktree.WorktreeError(
            f"git worktree prune failed (exit {proc.returncode}): {proc.stderr[-2000:]}"
        )


def _scrubbed(env: Mapping[str, str], names: frozenset[str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k not in names}


class WorktreeLauncher:
    """The intake Launcher for tasks that change the `source` git repository.

    `env` is the environment handed to the agent (`os.environ` when None), less the names in
    `scrub_env`. `exclude` lists paths, relative to the source root, left out of the diff.
    By default nothing is removed and nothing is left out.
    """

    def __init__(
        self,
        source: Path,
        agent: AgentRunner,
        *,
        env: Mapping[str, str] | None = None,
        exclude: Sequence[str] = (),
        scrub_env: Iterable[str] = (),
    ):
        self.source = Path(source)
        self.agent = agent
        self.env = env
        self.exclude = tuple(exclude)
        self.scrub_env = frozenset(scrub_env)
        self.created: list[Path] = []

    def launch(
        self,
        *,
        workspace: Path,
        prompt: str,
        install: Install | None,
        run_dir: Path,
        timeout: int,
    ) -> None:
        run_dir = Path(run_dir)
        wt = run_dir / worktree.WORKTREE_DIRNAME
        if wt.exists() or wt.is_symlink():
            worktree.remove_worktree(self.source, wt)
        else:
            # IntakeAdapter.execute deletes a stale run_dir with rmtree, which leaves git's
            # record of the worktree behind; `git worktree add` refuses that path until pruned.
            _prune(self.source)
        base = worktree.prepare_worktree(self.source, wt)
        if wt not in self.created:
            self.created.append(wt)
        env = _scrubbed(os.environ if self.env is None else self.env, self.scrub_env)
        self.agent.run(
            worktree=wt,
            prompt=WORKTREE_PREAMBLE + "\n\n" + prompt,
            install=install,
            run_dir=run_dir,
            timeout=timeout,
            env=env,
        )
        worktree.collect_diff(
            wt, base, Path(workspace) / "output" / worktree.DIFF_NAME, exclude=self.exclude
        )

    def cleanup(self) -> None:
        """Remove every worktree this launcher made; calling it again is harmless."""
        for wt in list(self.created):
            worktree.remove_worktree(self.source, wt)
        self.created.clear()


def _harness_prompt(prompt: str, install: Install | None) -> str:
    """The prompt as given, or the composed harness with the prompt at its placeholder."""
    if install is None:
        return prompt
    composed = compose_prompt(Path(install.staging_dir))
    head, sep, tail = composed.rpartition(_PLACEHOLDER)
    if not sep:
        raise ValueError("the composed harness prompt has no instruction placeholder")
    return head + prompt + tail


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait()


class OpencodeAgent:
    """OpenCode run with the worktree as its directory (an AgentRunner)."""

    def __init__(self, model: str, *, substrate: OpencodeSubstrate | None = None):
        self.model = model
        self.substrate = substrate if substrate is not None else OpencodeSubstrate()

    def run(
        self,
        *,
        worktree: Path,
        prompt: str,
        install: Install | None,
        run_dir: Path,
        timeout: int,
        env: Mapping[str, str],
    ) -> None:
        worktree = Path(worktree)
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        argv = self.substrate.run_argv(
            _harness_prompt(prompt, install), worktree, self.model, title=run_dir.name
        )
        child_env = self.substrate.run_env(env, worktree)
        events = run_dir / EVENTS_NAME
        try:
            with open(events, "wb") as out, open(run_dir / STDERR_NAME, "wb") as err:
                # Popen rather than subprocess.run: on a timeout the whole process group
                # (bun and what it started) must go, which needs the child's pid.
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    env=child_env,
                    start_new_session=True,
                )
                try:
                    rc = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    _kill_group(proc)
                    raise TimeoutError(f"opencode did not finish within {timeout}s") from exc
                except BaseException:
                    # KeyboardInterrupt and the like: the process group must not outlive us.
                    _kill_group(proc)
                    raise
            if rc != 0:
                raise RuntimeError(f"opencode exited with status {rc}")
        finally:
            self._write_trajectory(run_dir)

    def _write_trajectory(self, run_dir: Path) -> None:
        events = run_dir / EVENTS_NAME
        if events.is_file():
            text = self.substrate.read_trajectory(events, obs_trunc=4000, max_chars=200_000)
        else:
            text = "[ERROR] no events recorded"
        (run_dir / TRAJECTORY_NAME).write_text(text, encoding="utf-8")


__all__ = [
    "AgentRunner",
    "OpencodeAgent",
    "WORKTREE_PREAMBLE",
    "WorktreeLauncher",
]
