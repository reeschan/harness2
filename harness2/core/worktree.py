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

"""Git worktrees for code-change tasks: prepare one, take its diff, apply a diff back.

A code-change rollout works in its own git worktree of the source repository. The layout of
one run (`run_dir`) is:

- `run_dir/worktree/`: a detached worktree of the source. The agent works here.
- `run_dir/workspace/output/changes.diff`: the diff from the base commit (`git diff --binary`,
  without the paths the caller passes as `exclude`). It is not written when nothing changed.

The base commit is the source HEAD plus the tracked uncommitted changes (`git stash create`)
plus the untracked, not ignored files, committed once inside the worktree. So files the
caller keeps out of version control (its own tools, for example) are in the worktree too; the
caller keeps them out of the diff with `exclude` (paths relative to the source root).

Only the chosen run's diff goes back to the source (`apply_back`). Before it is applied, the
caller's `guard` sees the worktree and the source and can refuse with a reason. A guard should
trust only what it reads from the source: the agent can rewrite anything in the worktree. The
source's index and commits are never touched.

Git runs without a shell and with `git_env()`: the repository location variables of the
caller's environment are dropped, so every command talks to the repository it names. A
worktree shares the source's hooks, so the commands this module runs to make and fill a
worktree run with `core.hooksPath` pointed at an empty directory.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

WORKTREE_DIRNAME = "worktree"
DIFF_NAME = "changes.diff"

Guard = Callable[[Path, Path], str | None]
"""Called with (worktree, source) before a diff goes back: None to go on, or the reason not to."""

_IDENTITY = (
    "-c",
    "user.name=harness2",
    "-c",
    "user.email=harness2@localhost",
    "-c",
    "commit.gpgsign=false",
)
_STDERR_TAIL = 2000
_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
)


class WorktreeError(RuntimeError):
    """A git command failed, or the guard refused the diff."""


def git_env() -> dict[str, str]:
    """A copy of `os.environ` for running git: no repository location variables, no prompts.

    `GIT_DIR`, `GIT_WORK_TREE` and the like (set, for example, when running inside a git hook)
    would make git ignore `-C` and work on another repository, so they are dropped.
    """
    env = dict(os.environ)
    for name in _LOCATION_VARS:
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _fail(cmd: list[str], rc: int, stderr: str) -> WorktreeError:
    return WorktreeError(f"{' '.join(cmd)} failed (exit {rc}): {stderr[-_STDERR_TAIL:]}")


def _git(*args: str, cwd: Path | None = None) -> str:
    cmd = ["git", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=cwd, env=git_env())
    if proc.returncode != 0:
        raise _fail(cmd, proc.returncode, proc.stderr)
    return proc.stdout


def _git_bytes(*args: str) -> bytes:
    """Like `_git` but keeps stdout as bytes (a diff may hold text that is not UTF-8)."""
    cmd = ["git", *args]
    proc = subprocess.run(cmd, capture_output=True, check=False, env=git_env())
    if proc.returncode != 0:
        raise _fail(cmd, proc.returncode, proc.stderr.decode("utf-8", "replace"))
    return proc.stdout


@contextlib.contextmanager
def _no_hooks() -> Iterator[tuple[str, str]]:
    """Git options pointing `core.hooksPath` at an empty directory, removed afterwards."""
    empty = tempfile.mkdtemp(prefix="harness2-nohooks-")
    try:
        yield ("-c", f"core.hooksPath={empty}")
    finally:
        shutil.rmtree(empty, ignore_errors=True)


def _pathspec(exclude: Sequence[str]) -> list[str]:
    return ["--", ".", *(f":(exclude){p}" for p in exclude)]


def _z_names(out: str) -> list[str]:
    return sorted({n for n in out.split("\0") if n})


def _discard(source: Path, dest: Path) -> None:
    """Best effort: remove and unregister a half-made worktree. Never raises."""
    with contextlib.suppress(Exception):
        _git("-C", str(source), "worktree", "remove", "--force", str(dest))
    shutil.rmtree(dest, ignore_errors=True)
    with contextlib.suppress(Exception):
        _git("-C", str(source), "worktree", "prune")


def prepare_worktree(source: Path, dest: Path) -> str:
    """Make `dest` a detached worktree of `source` holding its current files; return the base.

    The base commit carries the source HEAD, the tracked uncommitted changes and the untracked,
    not ignored files. The source's index, HEAD, working tree and stash list stay as they are.
    No hooks run. When a step after `git worktree add` fails, the worktree is removed and
    unregistered before the error is raised.
    """
    source = Path(source)
    dest = Path(dest)
    try:
        top = _git("-C", str(source), "rev-parse", "--show-toplevel").strip()
    except WorktreeError as e:
        raise WorktreeError(f"{source} is not a git working tree: {e}") from e
    if not top or Path(top).resolve() != source.resolve():
        raise WorktreeError(f"{source} is not the root of a git working tree (root: {top})")
    if dest.exists() or dest.is_symlink():
        raise WorktreeError(f"worktree destination already exists: {dest}")

    rev = _git("-C", str(source), "stash", "create").strip() or "HEAD"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _no_hooks() as no_hooks:
        _git(*no_hooks, "-C", str(source), "worktree", "add", "--detach", str(dest), rev)
        try:
            _fill(source, dest, no_hooks)
            return _git("-C", str(dest), "rev-parse", "HEAD").strip()
        except BaseException:
            _discard(source, dest)
            raise


def _fill(source: Path, dest: Path, no_hooks: Sequence[str]) -> None:
    """Copy the source's untracked, not ignored files into `dest` and commit them there."""
    listed = _git("-C", str(source), "ls-files", "--others", "--exclude-standard", "-z")
    dest_abs = Path(os.path.abspath(dest))
    for rel in listed.split("\0"):
        if not rel:
            continue
        src_file = source / rel
        if Path(os.path.abspath(src_file)).is_relative_to(dest_abs):
            continue
        if src_file.is_symlink() or not src_file.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, target)

    _git(*no_hooks, "-C", str(dest), "add", "-A")
    _git(
        *_IDENTITY,
        *no_hooks,
        "-C",
        str(dest),
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "harness2 intake base",
    )


def collect_diff(
    worktree: Path, base_rev: str, out: Path, *, exclude: Sequence[str] = ()
) -> list[str]:
    """Write the worktree's changes since `base_rev` to `out`, leaving out `exclude`.

    `exclude` holds paths relative to the worktree root (files or directories); changes under
    them are not in the diff. By default every change is. Returns the changed paths, sorted.
    When nothing changed, any existing `out` (a symlink included) is removed and the result is
    `[]`. A symlink at `out` is replaced, never written through.
    """
    out = Path(out)
    wt = str(worktree)
    spec = _pathspec(exclude)
    with _no_hooks() as no_hooks:
        _git(*no_hooks, "-C", wt, "add", "-A")
    diff = _git_bytes("-C", wt, "diff", "--cached", "--binary", base_rev, *spec)
    if not diff:
        if out.is_symlink() or out.exists():
            out.unlink()
        return []
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_symlink():
        out.unlink()
    out.write_bytes(diff)
    names = _git("-C", wt, "diff", "--cached", "--name-only", "-z", base_rev, *spec)
    return _z_names(names)


def _diff_paths(source: Path, diff: Path) -> list[str]:
    """The paths a diff touches, read with `git apply --numstat -z`."""
    out = _git("-C", str(source), "apply", "--numstat", "-z", str(diff))
    tokens = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        parts = tok.split("\t", 2)
        if len(parts) != 3:
            raise WorktreeError(f"unexpected git apply --numstat output: {tok!r}")
        if parts[2]:
            paths.append(parts[2])
        else:  # a rename or copy: the source and destination follow as their own fields
            paths.extend(tokens[i : i + 2])
            i += 2
    return sorted(set(paths))


def _forbidden(path: str, exclude: Sequence[str]) -> bool:
    if path.startswith("/") or "\\" in path or ".." in path.split("/"):
        return True
    prefixes = [p.strip("/") for p in exclude]
    return any(path == p or path.startswith(p + "/") for p in prefixes if p)


def apply_back(
    source: Path,
    run_dir: Path,
    *,
    guard: Guard | None = None,
    exclude: Sequence[str] = (),
) -> list[str]:
    """Apply the run's diff to the source's working tree; return the changed paths, sorted.

    When `guard` is given, it is called first with the worktree and the source paths. If it
    returns a string, `WorktreeError` is raised with that reason; if it raises, the exception
    goes up as it is. A diff that touches a path outside the source, or a path under
    `exclude`, is refused too. On any failure the source is left as it was.
    """
    source = Path(source)
    run_dir = Path(run_dir)
    worktree = run_dir / WORKTREE_DIRNAME
    diff = run_dir / "workspace" / "output" / DIFF_NAME
    if not worktree.is_dir():
        raise WorktreeError(f"no worktree at {worktree}")
    if diff.is_symlink():
        raise WorktreeError(f"the diff is a symbolic link: {diff}")
    if not diff.is_file():
        raise WorktreeError(f"no diff at {diff}")

    if guard is not None:
        reason = guard(worktree, source)
        if reason is not None:
            raise WorktreeError(reason)

    diff_abs = str(diff.resolve())
    paths = _diff_paths(source, diff.resolve())
    bad = [p for p in paths if _forbidden(p, exclude)]
    if bad:
        raise WorktreeError(f"the diff touches paths it must not: {', '.join(bad)}")

    _git("-C", str(source), "apply", "--check", "--binary", diff_abs)
    _git("-C", str(source), "apply", "--binary", diff_abs)
    return paths


def remove_worktree(source: Path, worktree: Path) -> None:
    """Remove a worktree made by `prepare_worktree`. Nothing happens when it is gone."""
    worktree = Path(worktree)
    if not worktree.exists() and not worktree.is_symlink():
        return
    try:
        _git("-C", str(source), "worktree", "remove", "--force", str(worktree))
    except WorktreeError:
        shutil.rmtree(worktree, ignore_errors=True)
        _git("-C", str(source), "worktree", "prune")
