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

"""Intake adapter: tasks that arrive through a TaskConnector, seen as one benchmark.

`IntakeAdapter.ingest` polls the connector, normalizes each raw task into a TaskSpec and
stores it under `<root>`:

- `tasks/<task_id>/spec.json`  the task spec without `private_criteria`
- `tasks/<task_id>/files/`     the attachments, minus answer-key paths
- `private/<task_id>.json`     `{"private_criteria": [...]}`, read only by `score`
- `executions/<run_id>/`       one rollout: `workspace/` (with `output/`) and `trajectory.md`
- `base/`                      the unmodified harness tree

The rest of the class is the BenchmarkAdapter surface of `core/spec.py`. Running a rollout is
delegated to a `Launcher`; private criteria never reach the agent, the prompts or the
workspace.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import weakref
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from ..core import memory, schedule, tree
from ..core.intake import TaskConnector, TaskSpec, task_spec_from_dict, task_spec_to_dict
from ..core.normalize import NormalizeError, Normalizer, normalize_task
from ..core.proposer import Proposer
from ..core.spec import BenchPrompts, BenchSpec, ChannelSpec
from ..substrate.base import Install
from ..substrate.opencode import OpencodeSubstrate

DEFAULT_TIMEOUT = 3600
REASON_MAX = 2_000
DELIVERABLE_CAP = 60_000
PRIVATE_GLOB = "private_criteria*"
LEAK_GLOBS = ("private/**", PRIVATE_GLOB)

BASE_SYSTEMPROMPT = (
    "Complete the requested work inside the workspace. Put every deliverable file in the "
    "workspace's output/ directory; files left anywhere else are not handed back.\n"
)

Judge = Callable[[Path, str, Sequence[str]], "dict | None"]


@runtime_checkable
class Launcher(Protocol):
    """Runs one rollout: the agent works in `workspace` and writes its deliverables to
    `workspace/output/`; the launcher records the trajectory in `run_dir/trajectory.md`
    in the [ACTION]/[OBSERVATION]/[ERROR] grammar. `install` is None for the base harness.
    """

    def launch(
        self,
        *,
        workspace: Path,
        prompt: str,
        install: Install | None,
        run_dir: Path,
        timeout: int,
        task_id: str = "",
    ) -> None: ...


def _is_plain(path: str) -> bool:
    """True for a regular file or a real directory; False for links, fifos, sockets, devices."""
    return not os.path.islink(path) and (os.path.isfile(path) or os.path.isdir(path))


def _skip_links(directory: str, names: list[str]) -> list[str]:
    """A `shutil.copytree` ignore hook keeping only regular files and real directories."""
    return [n for n in names if not _is_plain(os.path.join(directory, n))]


def _drop_links(root: Path) -> None:
    """Remove every symlink and non-regular entry under `root` without following links."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + filenames:
            path = os.path.join(dirpath, name)
            if not _is_plain(path):
                os.unlink(path)
                if name in dirnames:
                    dirnames.remove(name)


def _is_leak(rel: str, patterns: Sequence[str]) -> bool:
    """Whether the relative path `rel` matches an answer-key glob (as core.context does)."""
    for pattern in patterns:
        pat = str(pattern).replace("\\", "/").lstrip("./")
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, f"*/{pat}"):
            return True
    return False


def _clip_reason(text: str) -> str:
    if len(text) <= REASON_MAX:
        return text
    suffix = " ... [truncated]"
    return text[: REASON_MAX - len(suffix)] + suffix


class IntakeAdapter:
    """The BenchmarkAdapter surface over tasks that a TaskConnector delivers."""

    def __init__(
        self,
        root: Path,
        connector: TaskConnector,
        normalizer: Normalizer,
        *,
        launcher: Launcher,
        substrate: Any = None,
        channel: ChannelSpec,
        judge: Judge | None = None,
        domains: Sequence[str] = (),
        executor: Any = None,
        model_connector: str = "",
        improver: Any = None,
    ):
        self.root = Path(root)
        self.connector = connector
        self.normalizer = normalizer
        self.launcher = launcher
        self.substrate = substrate if substrate is not None else OpencodeSubstrate()
        if channel.kind != "connector" and not str(channel.model).strip():
            raise ValueError(
                "IntakeAdapter needs a channel with a model (ChannelSpec.model is empty)"
            )
        self.judge = judge
        self.domains = tuple(domains)
        # The executor choose/record object of a model connector (None for the default one).
        # It keeps, per task, the candidates tried and how each rollout ended; `model_records`
        # turns that into the `models` payload of summary.json. An empty connector name means
        # the default connector, whose summary keeps the old shape (no `models` key).
        self.executor = executor
        self.model_connector = str(model_connector or "")
        # The last error of each rollout that raised (run_id -> "Type: message"). A base
        # rollout that never produced a run leaves no run directory to read, so the reason
        # the task connector receives comes from here (see `_deliverable_dir`).
        self.rollout_errors: dict[str, str] = {}
        # The improver's ModelSession (None unless a model connector runs the improver
        # sessions); its per-task rows go to `models.improver` of summary.json.
        self.improver = improver
        self.spec = BenchSpec(
            name="intake",
            runs_root=self.root / "runs",
            install_mode="workspace_config",
            prompts=BenchPrompts(
                work="complete the requested work and place the result in output/"
            ),
            channels={"proposer": channel, "selector": channel},
            leak_globs=LEAK_GLOBS,
        )
        surface = self.substrate.surface()
        base = {name: "" for name in surface.always_on}
        base["systemprompt.md"] = BASE_SYSTEMPROMPT
        tree.write_tree(surface, base, self.base_harness(), stubs=False)

    # -- storage ---------------------------------------------------------------------------

    def _task_dir(self, task_id: str) -> Path:
        return self.root / "tasks" / task_id

    def _private_path(self, task_id: str) -> Path:
        return self.root / "private" / f"{task_id}.json"

    def _load(self, task_id: str) -> TaskSpec:
        path = self._task_dir(task_id) / "spec.json"
        return task_spec_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def known_domains(self) -> list[str]:
        """The constructor's domains plus those of every stored task, sorted, no duplicates."""
        found = set(self.domains)
        for task_id in self.tasks():
            try:
                found.add(self._load(task_id).domain)
            except (OSError, ValueError):
                continue
        return sorted(found)

    # -- intake ----------------------------------------------------------------------------

    def ingest(self) -> list[str]:
        """Poll the connector and store every new task; return the task ids stored now.

        A task that fails (normalization, a task_id that differs from the raw id, fetching
        its files, any other Exception) is reported with `connector.fail` and skipped.
        A task whose spec.json already exists is skipped without normalizing it again.
        """
        stored: list[str] = []
        for raw in self.connector.poll():
            if self._has_spec(raw.id):
                continue
            try:
                spec = normalize_task(self.normalizer, raw, domains=self.known_domains())
                if spec.task_id != raw.id:
                    raise ValueError(
                        f"the normalized task_id {spec.task_id!r} differs from the incoming "
                        f"task id {raw.id!r}; the task_id must equal the incoming id"
                    )
                self._store(raw, spec)
            except NormalizeError as exc:
                self.connector.fail(raw.id, _clip_reason("; ".join(exc.problems) or str(exc)))
                continue
            except Exception as exc:
                self.connector.fail(
                    raw.id, _clip_reason(f"intake failed: {type(exc).__name__}: {exc}")
                )
                continue
            stored.append(spec.task_id)
        return stored

    def _has_spec(self, raw_id: str) -> bool:
        if not raw_id or "/" in raw_id or "\\" in raw_id or raw_id in (".", ".."):
            return False
        return (self._task_dir(raw_id) / "spec.json").is_file()

    def _store(self, raw, spec: TaskSpec) -> None:
        """Write files/ and private/ first and spec.json last: spec.json marks a stored task."""
        task_dir = self._task_dir(spec.task_id)
        if task_dir.exists():
            shutil.rmtree(task_dir)
        files = task_dir / "files"
        files.mkdir(parents=True)
        try:
            self.connector.fetch_files(raw, files)
            self._drop_leaks(files)
            private = self._private_path(spec.task_id)
            private.parent.mkdir(parents=True, exist_ok=True)
            private.write_text(
                json.dumps({"private_criteria": list(spec.private_criteria)}, indent=1),
                encoding="utf-8",
            )
            (task_dir / "spec.json").write_text(
                json.dumps(task_spec_to_dict(spec, include_private=False), indent=1),
                encoding="utf-8",
            )
        except BaseException:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise

    def _drop_leaks(self, files: Path) -> None:
        _drop_links(files)
        for path in sorted(files.rglob("*"), reverse=True):
            rel = path.relative_to(files).as_posix()
            if not _is_leak(rel, self.spec.leak_globs):
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()

    # -- BenchmarkAdapter surface ----------------------------------------------------------

    def tasks(self, split: str = "all") -> list[str]:
        if split != "all":
            raise ValueError(f"the intake adapter has only the 'all' split, got {split!r}")
        tasks_dir = self.root / "tasks"
        if not tasks_dir.is_dir():
            return []
        return sorted(p.name for p in tasks_dir.iterdir() if (p / "spec.json").is_file())

    def domain_of(self, task_id: str) -> str:
        return self._load(task_id).domain

    def task_prompt(self, task_id: str) -> str:
        spec = self._load(task_id)
        where = f"output/{spec.deliverable_where}" if spec.deliverable_where else "output/"
        return (
            f"{spec.prompt.rstrip()}\n\n"
            f"Deliverable: {spec.deliverable_what.strip()}. Place it in `{where}` in the "
            f"workspace."
        )

    def prompts_for(self, task_id: str) -> BenchPrompts:
        return self._load(task_id).prompts

    def task_files(self, task_id: str) -> Path | None:
        files = self._task_dir(task_id) / "files"
        return files if files.is_dir() else None

    def base_harness(self) -> Path:
        return self.root / "base"

    def validate_tree(self, tree: dict[str, str]) -> None:
        """A generic workspace has no harness invariants of its own."""
        return None

    def model_records(self, task_id: str) -> tuple[dict, str]:
        """The executor `models` payload and connector name for `task_id`'s summary.

        Empty for the default connector, so that summary.json keeps its old shape. With a
        model connector the payload is `{"executor": [...]}`, the candidates in the order they
        were first tried (see `intake_worktree.ExecutorModels.records_for`). When a model
        connector ran improver sessions, `"improver": [...]` follows in the same form.
        """
        if not self.model_connector:
            return {}, ""
        payload: dict = {}
        if self.executor is not None:
            rows = self.executor.records_for(task_id)
            if rows:
                payload["executor"] = rows
        if self.improver is not None:
            rows = self.improver.records_for(task_id)
            if rows:
                payload["improver"] = rows
        return payload, self.model_connector

    def execute(
        self, task_id: str, harness_dir: Path | None, run_id: str, *, timeout: int | None = None
    ) -> Path:
        run_dir = self.root / "executions" / run_id
        if (run_dir / ".complete").is_file():
            return run_dir
        if run_dir.exists():
            shutil.rmtree(run_dir)
        workspace = run_dir / "workspace"
        source = self.task_files(task_id)
        if source is not None and not source.is_symlink():
            shutil.copytree(
                source,
                workspace,
                symlinks=True,
                ignore=lambda d, names: (
                    _skip_links(d, names)
                    + [
                        n
                        for n in names
                        if _is_leak(
                            (Path(d) / n).relative_to(source).as_posix(), self.spec.leak_globs
                        )
                    ]
                ),
            )
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "output").mkdir(exist_ok=True)
        prompt = self.task_prompt(task_id)
        install = (
            None
            if harness_dir is None
            else self.substrate.materialise(Path(harness_dir), self.spec, {})
        )
        try:
            self.launcher.launch(
                workspace=workspace,
                prompt=prompt,
                install=install,
                run_dir=run_dir,
                timeout=int(timeout or DEFAULT_TIMEOUT),
                task_id=task_id,
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.rollout_errors[str(run_id)] = message
            raise RuntimeError(
                f"intake rollout {run_id} for task {task_id!r} failed: {message}"
            ) from exc
        finally:
            if install is not None:
                shutil.rmtree(install.staging_dir, ignore_errors=True)
        (run_dir / ".complete").write_text(run_id + "\n", encoding="utf-8")
        return run_dir

    def _output_files(self, run_dir: Path) -> list[tuple[str, Path]]:
        """Regular files under workspace/output/, never through a symbolic link."""
        output = Path(run_dir) / "workspace" / "output"
        if output.is_symlink() or not output.is_dir():
            return []
        found: list[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(output, followlinks=False):
            dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
            for name in filenames:
                path = Path(dirpath) / name
                if path.is_symlink() or not path.is_file():
                    continue
                found.append((path.relative_to(output).as_posix(), path))
        return sorted(found)

    def delivered(self, run_dir: Path) -> bool:
        return bool(self._output_files(run_dir))

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        path = Path(run_dir) / "trajectory.md"
        if not path.is_file():
            return "[ERROR] no trajectory recorded"
        return path.read_text(encoding="utf-8", errors="replace")[: obs_trunc * 4]

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for rel, path in self._output_files(run_dir):
            data = path.read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
            out[rel] = text[:DELIVERABLE_CAP]
        return out

    def deliverable_text(self, run_dir: Path) -> str:
        return "\n\n".join(
            f"=== {rel} ===\n{text}" for rel, text in self.deliverable_files(run_dir).items()
        )

    def score(self, run_dir: Path, task_id: str) -> dict | None:
        if self.judge is None:
            return None
        path = self._private_path(task_id)
        criteria: list[str] = []
        if path.is_file():
            criteria = list(
                json.loads(path.read_text(encoding="utf-8")).get("private_criteria", [])
            )
        return self.judge(Path(run_dir), task_id, criteria)


# The task ids each adapter has already taken up in this process. A later run_intake skips
# them; across processes, the `tasks/<task_id>/.handled` mark (see below) covers them.
_HANDLED: "weakref.WeakKeyDictionary[IntakeAdapter, set[str]]" = weakref.WeakKeyDictionary()

# The file under `tasks/<task_id>/` that records the task went back to the connector:
# one word, "delivered" or "failed", written only after connector.deliver / connector.fail
# returned without an exception.
HANDLED_MARK = ".handled"


def _is_marked_handled(adapter: IntakeAdapter, task_id: str) -> bool:
    """Whether the task carries a `.handled` mark (whatever its word): it is not run again."""
    path = adapter._task_dir(task_id) / HANDLED_MARK
    return path.is_file() or path.is_symlink()


def _write_handled_mark(adapter: IntakeAdapter, task_id: str, word: str) -> None:
    (adapter._task_dir(task_id) / HANDLED_MARK).write_text(word, encoding="utf-8")


def _deliverable_dir(adapter: IntakeAdapter, res: schedule.TaskResult, mode: str) -> Path:
    """The chosen run's `workspace/output/`; ValueError (the fail reason) when it cannot go."""
    selection = (res.selections or {}).get(mode) or {}
    chosen = str(selection.get("chosen_run_dir") or "")
    if not chosen:
        # Carry the rollout's own errors (e.g. "no executor model for substrate opencode")
        # into the reason the task connector receives, not just the generic status line. The
        # base rollouts that raised left no run directory behind, so their errors come from
        # the adapter's record of this run; `schedule` is not touched for this.
        detail = [str(e) for e in (res.errors or ())]
        errors = getattr(adapter, "rollout_errors", {})
        for entry in res.base or ():
            if not isinstance(entry, dict):
                continue
            run_id = str(entry.get("run_id") or "")
            error = errors.get(run_id, "")
            if error:
                detail.append(f"{entry.get('label') or run_id}: {error}")
        suffix = f": {'; '.join(detail)}" if detail else ""
        raise ValueError(f"no run was selected for mode {mode!r} (status {res.status}){suffix}")
    output = Path(chosen) / "workspace" / "output"
    if output.is_symlink():
        raise ValueError(f"the selected run's workspace/output is a symbolic link: {output}")
    if not output.is_dir():
        raise ValueError(f"the selected run has no workspace/output directory: {output}")
    if not adapter._output_files(Path(chosen)):
        raise ValueError(f"the selected run's workspace/output holds no file: {output}")
    return output


def _model_records(adapter: IntakeAdapter, task_id: str) -> tuple[dict, str]:
    """The `models` payload and connector name for a delivered summary; empty when absent."""
    getter = getattr(adapter, "model_records", None)
    if not callable(getter):
        return {}, ""
    try:
        payload, name = getter(task_id)
    except Exception:
        return {}, ""
    return dict(payload or {}), str(name or "")


def _check_run_args(
    k: int,
    modes: tuple[str, ...],
    library: memory.MemoryConnector | None,
    libraries: dict[str, memory.MemoryConnector] | None,
) -> None:
    """The caller errors run_task would raise, raised before any task is touched.

    A copy of run_task's own argument checks, so that a wrong argument is the caller's
    ValueError and never a connector-side failure of every task in the batch.
    """
    if int(k) < 1:
        raise ValueError(f"k must be positive, got {k!r}")
    if not modes or len(set(modes)) != len(modes):
        raise ValueError("modes must contain at least one distinct recursion mode")
    unknown = sorted(set(modes) - set(schedule.MODES))
    if unknown:
        raise ValueError(f"unknown modes {unknown}; expected a subset of {schedule.MODES}")
    if library is not None:
        if libraries:
            raise ValueError("pass either library or libraries, not both")
        if len(modes) != 1:
            raise ValueError(
                "a shared mutable library would leak one mode into another; "
                "pass libraries={'agg': ..., 'seq': ...} instead"
            )


def _set_improver_task(task_id: str, *props: Proposer | None) -> None:
    """Tell the ModelSession of each proposer (once per session) which task is current."""
    seen: set[int] = set()
    for prop in props:
        session = getattr(prop, "models", None)
        if session is None or id(session) in seen:
            continue
        seen.add(id(session))
        session.for_task(task_id)


def run_intake(
    adapter: IntakeAdapter,
    prop: Proposer,
    *,
    k: int,
    runs_root: Path,
    modes: Sequence[str] = schedule.MODES,
    seed: int | None = None,
    tag: str = "",
    selector_prop: Proposer | None = None,
    library: memory.MemoryConnector | None = None,
    libraries: dict[str, memory.MemoryConnector] | None = None,
) -> list[schedule.TaskResult]:
    """Ingest new tasks, run each through `schedule.run_task`, and hand the result back.

    For every stored task this adapter has not yet taken up in this process and that has
    no `tasks/<task_id>/.handled` mark, run_task runs with the given arguments. The mark
    holds "delivered" or "failed" and is written only after `connector.deliver` or
    `connector.fail` returned without an exception, so a restarted process does not hand
    the same task to the connector twice. The chosen run of `modes[0]` is delivered with
    `adapter.connector.deliver(task_id, <its workspace/output/>, res.to_dict())` when that
    directory is a real directory (not a symbolic link) holding at least one regular file;
    otherwise the task goes to `adapter.connector.fail`. An Exception from run_task or from
    the delivery is reported with `connector.fail` and the task is left out of the returned
    list; the other tasks go on. Caller errors that run_task would reject (`k < 1`, empty,
    repeated or unknown modes, `library` with `libraries` or with more than one mode) raise
    ValueError before `ingest`, so the connector is left untouched.

    The adapter's normalizer runs inside `ingest`: a normalizer built on a Proposer must use
    one without an `audit_dir`, or with one under `<root>/private/`, because the audit trail
    records the mission and the reply, which hold the private criteria.
    """
    modes = tuple(modes)
    _check_run_args(k, modes, library, libraries)
    handled = _HANDLED.setdefault(adapter, set())
    adapter.ingest()
    results: list[schedule.TaskResult] = []
    for task_id in adapter.tasks():
        if task_id in handled:
            continue
        handled.add(task_id)
        if _is_marked_handled(adapter, task_id):
            continue
        # Tell the improver sessions (proposer and selector) which task they now work for.
        _set_improver_task(task_id, prop, selector_prop)
        try:
            res = schedule.run_task(
                task_id,
                k,
                adapter=adapter,
                prop=prop,
                runs_root=runs_root,
                modes=modes,
                seed=seed,
                tag=tag,
                selector_prop=selector_prop,
                library=library,
                libraries=libraries,
            )
        except Exception as exc:
            adapter.connector.fail(
                task_id, _clip_reason(f"run_task failed: {type(exc).__name__}: {exc}")
            )
            _write_handled_mark(adapter, task_id, "failed")
            continue
        # A skipped (resumed) task keeps the executor record stored on its old summary; this
        # process has run no rollout, so the adapter offers nothing to replace it with.
        payload, name = _model_records(adapter, task_id)
        if payload:
            res.models = payload
        if name:
            res.model_connector = name
        try:
            output = _deliverable_dir(adapter, res, modes[0])
        except ValueError as exc:
            adapter.connector.fail(task_id, _clip_reason(str(exc)))
            _write_handled_mark(adapter, task_id, "failed")
            results.append(res)
            continue
        try:
            adapter.connector.deliver(task_id, output, res.to_dict())
        except Exception as exc:
            adapter.connector.fail(
                task_id, _clip_reason(f"delivery failed: {type(exc).__name__}: {exc}")
            )
            _write_handled_mark(adapter, task_id, "failed")
            continue
        _write_handled_mark(adapter, task_id, "delivered")
        results.append(res)
    return results


__all__ = ["IntakeAdapter", "Launcher", "Judge", "LEAK_GLOBS", "run_intake"]
