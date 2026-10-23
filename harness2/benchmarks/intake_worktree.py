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

import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from ..core import models, worktree
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
    `run_dir/trajectory.md`. Raises on failure or when `timeout` (seconds) runs out.

    `reset`, when given, puts the worktree back at the base commit before the agent tries
    again after a failed rollout.
    """

    def run(
        self,
        *,
        worktree: Path,
        prompt: str,
        install: Install | None,
        run_dir: Path,
        timeout: int,
        env: Mapping[str, str],
        task_id: str = "",
        reset: Callable[[], None] | None = None,
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


def _reset_worktree(wt: Path, base: str) -> None:
    """Put the worktree `wt` back at `base`: drop every change and untracked file.

    A rollout that failed because the model could not be used is tried again from the base
    tree, so the half-written work of the failed attempt cannot leak into the retry's diff.
    """
    for args in (("reset", "--hard", base), ("clean", "-fdx")):
        proc = subprocess.run(
            ["git", "-C", str(wt), *args],
            capture_output=True,
            text=True,
            check=False,
            env=worktree.git_env(),
        )
        if proc.returncode != 0:
            raise worktree.WorktreeError(
                f"git {' '.join(args)} in the worktree failed (exit {proc.returncode}): "
                f"{proc.stderr[-2000:]}"
            )


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
        task_id: str = "",
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
            task_id=task_id,
            reset=lambda: _reset_worktree(wt, base),
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


# The words of an error's own message that mean the model (not the task) could not be used: a
# usage limit, authentication, or a model/variant that does not exist. Broad words such as
# "provider", "connection", "network", "region" and numeric server errors are NOT here: they
# match ordinary failures and must not move the rollout to the next candidate. A timeout is
# not here either (the caller raises it before this check).
UNAVAILABLE_TERMS = (
    "rate limit",
    "too many requests",
    "usage limit",
    "quota",
    "insufficient balance",
    "invalid api key",
    "unauthorized",
    "variant unavailable",
    "model not found",
)
# The error names (`error.name`) with the same meaning.
UNAVAILABLE_ERROR_NAMES = (
    "ProviderAuthError",
    "ProviderAuthenticationError",
    "ProviderModelNotFoundError",
    "ProviderRateLimitError",
    "ProviderQuotaExceededError",
    "ProviderUsageLimitError",
)
# The numeric status codes (`error.data.statusCode`) with the same meaning. A status written as
# a string ("429") is not one of these.
UNAVAILABLE_CODES = (401, 403, 429, 529)


def _events(path: Path) -> list[dict]:
    """The JSON object events of an `opencode run --format json` stream; bad lines are skipped."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            found.append(event)
    return found


def _error_objects(event: dict) -> list[dict]:
    """The error objects of an error event: `event.error` and the error in its `part`.

    When the error event carries no nested object, the event (or its part) itself is the
    object, so its `name`/`message` are still read. Nothing else of the event is inspected.
    """
    found: list[dict] = []
    part = event.get("part")
    holders: list[dict] = []
    if event.get("type") == "error":
        holders.append(event)
    if isinstance(part, dict) and part.get("type") == "error":
        holders.append(part)
    for holder in holders:
        error = holder.get("error")
        found.append(error if isinstance(error, dict) else holder)
    return found


def _number(value: Any) -> float | None:
    """`value` as a number, or None when it is not one (a bool or a str is not)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _error_fields(error: dict) -> tuple[str, list[str], float | None, bool | None]:
    """The fixed fields of an error object: its name, message strings, status code and
    `data.isRetryable`.

    The message is `data.message`, and `message` when that is missing. `isRetryable` is read
    as one of the fixed fields but is never a reason by itself: a retryable transient server
    error is still not a model that cannot be used.
    """
    name = error.get("name") if isinstance(error.get("name"), str) else ""
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    messages = [
        text for text in (data.get("message"), error.get("message")) if isinstance(text, str)
    ]
    retryable = data.get("isRetryable")
    return (
        name,
        messages,
        _number(data.get("statusCode")),
        retryable if isinstance(retryable, bool) else None,
    )


def is_unavailable(returncode: int, events_path: Path) -> bool:
    """Whether a failed opencode run failed because its model could not be used.

    True only for a non-zero exit with an error event whose fixed fields name a usage limit,
    authentication or an unavailable model/variant: the error's `name` (an explicit constant),
    its numeric `data.statusCode` (401, 403, 429, 529) or its `data.message`/`message` holding
    a specific phrase. The rest of the event (headers, provider ids, token limits, other
    numbers) is not inspected, so an ordinary failure is not mistaken for a missing model.
    A timeout (raised by the caller, not here) and any other error are not unavailable.
    """
    if returncode == 0:
        return False
    for event in _events(Path(events_path)):
        for error in _error_objects(event):
            name, messages, code, _retryable = _error_fields(error)
            if name in UNAVAILABLE_ERROR_NAMES:
                return True
            if code is not None and int(code) in UNAVAILABLE_CODES:
                return True
            for text in messages:
                lowered = text.lower()
                if any(term in lowered for term in UNAVAILABLE_TERMS):
                    return True
    return False


def usage(events_path: Path) -> tuple[float, models.Tokens]:
    """Sum the cost and the input/output tokens over the step_finish events of a run."""
    cost = 0.0
    input_tokens: list[int] = []
    output_tokens: list[int] = []
    for event in _events(Path(events_path)):
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if (part.get("type") or event.get("type")) not in ("step-finish", "step_finish"):
            continue
        value = part.get("cost", event.get("cost"))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            cost += float(value)
        tokens = part.get("tokens") or event.get("tokens") or {}
        if not isinstance(tokens, dict):
            continue
        for name, found in (("input", input_tokens), ("output", output_tokens)):
            count = tokens.get(name)
            if isinstance(count, int) and not isinstance(count, bool):
                found.append(count)
    return (
        round(cost, 6),
        models.Tokens(
            input=sum(input_tokens) if input_tokens else None,
            output=sum(output_tokens) if output_tokens else None,
        ),
    )


class ExecutorModels:
    """The executor candidates of a model connector, walked per task.

    `routes(Role.EXECUTOR, task_id=..., substrate=...)` is called once per task and the list
    remembered, including an empty one. A candidate whose runner is not `substrate` cannot run
    here: it is left out of the runnable candidates and kept as a summary row with a
    `skipped_reason` (`n` stays 0). `current` is the candidate to run now; `advance` moves past
    the candidate that failed (False when there is none after it). Every attempt is aggregated
    per candidate, in the order first tried, and handed to the connector's `record` when it has
    one.

    `override` replaces the candidates of every task with that single `ModelChoice`; it is how
    `--model` keeps its old meaning with the default connector. `record` overrides the
    connector's own `record` capability (mostly for tests); a failing record is a warning on
    stderr, never a failed rollout.
    """

    def __init__(
        self,
        connector: Any,
        *,
        substrate: str = "opencode",
        name: str = "",
        override: "models.ModelChoice | None" = None,
        record: Any = None,
    ):
        self.connector = connector
        self.substrate = substrate
        self.name = name or models.DEFAULT
        self.override = override
        capabilities = models.capabilities(connector)
        self._record = capabilities.record if record is None else record
        self._lock = threading.Lock()
        self._choices: dict[str, list[models.ModelChoice]] = {}
        self._index: dict[str, int] = {}
        self._records: dict[str, list[dict]] = {}
        self._skipped: dict[str, list[dict]] = {}
        self._other_runners: dict[str, int] = {}

    def _for_task(self, task_id: str) -> list[models.ModelChoice]:
        """The runnable candidates of `task_id`, asked of the connector the first time only."""
        if task_id not in self._choices:
            offered = (
                [self.override]
                if self.override is not None
                else list(
                    self.connector.routes(
                        models.Role.EXECUTOR, task_id=task_id, substrate=self.substrate
                    )
                )
            )
            runnable: list[models.ModelChoice] = []
            skipped: list[models.ModelChoice] = []
            for choice in offered:
                if choice.runner == self.substrate:
                    runnable.append(choice)
                else:
                    skipped.append(choice)
            self._choices[task_id] = runnable
            self._other_runners[task_id] = len(skipped)
            for choice in skipped:
                self._skip_row(task_id, choice)
        return self._choices[task_id]

    def current(self, task_id: str) -> models.ModelChoice:
        """The candidate to run now; RuntimeError when the connector offered none."""
        with self._lock:
            choices = self._for_task(task_id)
            if not choices:
                others = self._other_runners.get(task_id, 0)
                suffix = f" ({others} candidates for other runners)" if others else ""
                raise RuntimeError(
                    f"no executor model for substrate {self.substrate} "
                    f"from model connector {self.name}{suffix}"
                )
            index = min(self._index.get(task_id, 0), len(choices) - 1)
            return choices[index]

    def advance(self, task_id: str, failed: models.ModelChoice) -> bool:
        """Move past `failed`; False when the candidates are used up.

        Only the rollout whose candidate is still the current one moves the position. A
        rollout whose candidate another rollout has already moved past does not move it again
        (a parallel rollout that failed on the same candidate would otherwise skip the
        candidate no one tried); it runs the candidate now current. The read and the move are
        under the lock.
        """
        with self._lock:
            choices = self._for_task(task_id)
            if not choices:
                return False
            index = min(self._index.get(task_id, 0), len(choices) - 1)
            if choices[index] != failed:
                return True
            if index + 1 >= len(choices):
                return False
            self._index[task_id] = index + 1
            return True

    def settle(
        self,
        task_id: str,
        choice: models.ModelChoice,
        *,
        ok: bool,
        unavailable: bool = False,
        cost_usd: float = 0.0,
        tokens: models.Tokens | None = None,
        error: str = "",
        run_id: str = "",
    ) -> None:
        """Record one attempt in the summary row and hand it to the connector's `record`."""
        outcome = models.Outcome(
            ok=bool(ok),
            unavailable=bool(unavailable),
            cost_usd=float(cost_usd or 0.0),
            tokens=tokens or models.Tokens(),
            error=str(error or ""),
            run_id=str(run_id or ""),
        )
        with self._lock:
            row = self._row(task_id, choice)
            row["n"] += 1
            row["ok"] = row["ok"] or outcome.ok
            row["unavailable"] = row["unavailable"] or outcome.unavailable
            row["cost_usd"] = round(row["cost_usd"] + outcome.cost_usd, 6)
            for field in ("input", "output"):
                value = getattr(outcome.tokens, field, None)
                if value is not None:
                    row["tokens"][field] = (row["tokens"][field] or 0) + int(value)
        if self._record is None:
            return
        try:
            self._record(models.Role.EXECUTOR, task_id, choice, outcome)
        except Exception as exc:
            print(
                f"warning: model connector record failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    @staticmethod
    def _new_row(choice: models.ModelChoice) -> dict:
        return {
            "runner": choice.runner,
            "model": choice.model,
            "effort": choice.effort,
            "ok": False,
            "unavailable": False,
            "cost_usd": 0.0,
            "tokens": {"input": None, "output": None},
            "n": 0,
        }

    def _row(self, task_id: str, choice: models.ModelChoice) -> dict:
        rows = self._records.setdefault(task_id, [])
        for row in rows:
            if (row["runner"], row["model"], row["effort"]) == (
                choice.runner,
                choice.model,
                choice.effort,
            ):
                return row
        row = self._new_row(choice)
        rows.append(row)
        return row

    def _skip_row(self, task_id: str, choice: models.ModelChoice) -> dict:
        """A row for a candidate that cannot run on this substrate (`n` stays 0)."""
        rows = self._skipped.setdefault(task_id, [])
        for row in rows:
            if (row["runner"], row["model"], row["effort"]) == (
                choice.runner,
                choice.model,
                choice.effort,
            ):
                return row
        row = self._new_row(choice)
        row["skipped_reason"] = f"runner {choice.runner} is not {self.substrate}"
        rows.append(row)
        return row

    def records_for(self, task_id: str) -> list[dict]:
        """The summary rows of `task_id`, copies: the candidates tried (in the order first
        tried), then the candidates left out because their runner is not this substrate."""
        with self._lock:
            rows = [
                {**row, "tokens": dict(row.get("tokens") or {})}
                for row in self._records.get(task_id, [])
            ]
            rows.extend(
                {**row, "tokens": dict(row.get("tokens") or {})}
                for row in self._skipped.get(task_id, [])
            )
            return rows


class OpencodeAgent:
    """OpenCode run with the worktree as its directory (an AgentRunner).

    `model` is either a fixed model string, or an `ExecutorModels` that picks the executor
    candidate per task and switches to the next one when opencode cannot use the current
    model. In the second case a failed run is retried once, in the same run directory and from
    the base worktree (`reset`), with the candidate now current; the connector's `record` and
    the summary rows see every attempt. The failed attempt's `events.N.jsonl`,
    `opencode.N.stderr` and `trajectory.N.md` stay beside the last attempt's files.
    """

    # One rollout is tried at most this many times: the first failure may move to the next
    # candidate, a second unavailable failure ends the rollout.
    MAX_ATTEMPTS = 2

    def __init__(
        self, model: "str | ExecutorModels", *, substrate: OpencodeSubstrate | None = None
    ):
        self.model = model
        self.substrate = substrate if substrate is not None else OpencodeSubstrate()

    def _choice(self, task_id: str) -> models.ModelChoice:
        if isinstance(self.model, str):
            return models.ModelChoice(runner="opencode", model=self.model)
        return self.model.current(task_id)

    def _advance(self, task_id: str, failed: models.ModelChoice) -> bool:
        return False if isinstance(self.model, str) else self.model.advance(task_id, failed)

    def _settle(self, task_id: str, choice: models.ModelChoice, run_dir: Path, **kwargs) -> None:
        if isinstance(self.model, str):
            return
        cost, tokens = usage(Path(run_dir) / EVENTS_NAME)
        self.model.settle(
            task_id,
            choice,
            cost_usd=cost,
            tokens=tokens,
            run_id=Path(run_dir).name,
            **kwargs,
        )

    def run(
        self,
        *,
        worktree: Path,
        prompt: str,
        install: Install | None,
        run_dir: Path,
        timeout: int,
        env: Mapping[str, str],
        task_id: str = "",
        reset: Callable[[], None] | None = None,
    ) -> None:
        worktree = Path(worktree)
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        child_env = self.substrate.run_env(env, worktree)
        events = run_dir / EVENTS_NAME
        attempt = 1
        while True:
            choice = self._choice(task_id)
            argv = self.substrate.run_argv(
                _harness_prompt(prompt, install),
                worktree,
                choice.model,
                title=run_dir.name,
                variant=choice.effort or None,
            )
            try:
                returncode = self._run_once(argv, run_dir, events, timeout, child_env)
            except BaseException as exc:
                self._settle(
                    task_id,
                    choice,
                    run_dir,
                    ok=False,
                    unavailable=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            unavailable = is_unavailable(returncode, events)
            self._settle(
                task_id,
                choice,
                run_dir,
                ok=returncode == 0,
                unavailable=unavailable,
                error="" if returncode == 0 else f"opencode exited with status {returncode}",
            )
            if returncode == 0:
                return
            if not unavailable:
                raise RuntimeError(f"opencode exited with status {returncode}")
            # Move the task's position even when this rollout has no retry left, so the
            # rollouts still to come run the next candidate. A position another rollout
            # already moved is not moved again; the retry runs the candidate now current.
            advanced = self._advance(task_id, choice)
            if attempt >= self.MAX_ATTEMPTS or not advanced:
                raise RuntimeError(f"opencode exited with status {returncode}")
            # Keep the failed attempt's trace, then retry from the base worktree so that its
            # half-written changes cannot leak into the retry's diff.
            self._archive(run_dir, attempt)
            attempt += 1
            if reset is not None:
                reset()

    def _archive(self, run_dir: Path, attempt: int) -> None:
        """Keep the trace of failed attempt `attempt` beside the next attempt's files."""
        for name, archived in (
            (EVENTS_NAME, f"events.{attempt}.jsonl"),
            (STDERR_NAME, f"opencode.{attempt}.stderr"),
            (TRAJECTORY_NAME, f"trajectory.{attempt}.md"),
        ):
            source = Path(run_dir) / name
            if source.is_file() or source.is_symlink():
                source.replace(Path(run_dir) / archived)

    def _run_once(
        self,
        argv: list[str],
        run_dir: Path,
        events: Path,
        timeout: int,
        env: Mapping[str, str],
    ) -> int:
        try:
            with open(events, "wb") as out, open(run_dir / STDERR_NAME, "wb") as err:
                # Popen rather than subprocess.run: on a timeout the whole process group
                # (bun and what it started) must go, which needs the child's pid.
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    env=env,
                    start_new_session=True,
                )
                try:
                    return proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    _kill_group(proc)
                    raise TimeoutError(f"opencode did not finish within {timeout}s") from exc
                except BaseException:
                    # KeyboardInterrupt and the like: the process group must not outlive us.
                    _kill_group(proc)
                    raise
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
    "ExecutorModels",
    "OpencodeAgent",
    "WORKTREE_PREAMBLE",
    "WorktreeLauncher",
    "UNAVAILABLE_CODES",
    "UNAVAILABLE_ERROR_NAMES",
    "UNAVAILABLE_TERMS",
    "is_unavailable",
    "usage",
]
