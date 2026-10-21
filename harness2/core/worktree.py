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

A code-change (PBI) rollout works in its own git worktree of the source repository. The layout
of one run (`run_dir`) is:

- `run_dir/worktree/`: a detached worktree of the source. The agent works here.
- `run_dir/workspace/output/changes.diff`: the diff from the base commit (`git diff --binary`,
  without `docs/pbi/`). It is not written when nothing changed.

The base commit is the source HEAD plus the tracked uncommitted changes (`git stash create`)
plus the untracked, not ignored files (including `docs/pbi/`), committed once inside the
worktree. So the verification tools under `docs/pbi/tools/` work in the worktree too, and
never show up in the diff.

Only the chosen run's diff goes back to the source (`apply_back`), after the PBI's untouched
files in the worktree are checked. The check runs the source's `docs/pbi/tools/unchanged.py`
against the source's record (`--dir <source>/docs/pbi`), never the copies inside the worktree:
the agent can rewrite those, and `docs/pbi/` never shows up in the diff. The source's index and
commits are never touched. Git runs without a shell.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

WORKTREE_DIRNAME = "worktree"
DIFF_NAME = "changes.diff"
EXCLUDE = ("docs/pbi",)

_IDENTITY = (
    "-c",
    "user.name=harness2",
    "-c",
    "user.email=harness2@localhost",
    "-c",
    "commit.gpgsign=false",
)
_STDERR_TAIL = 2000


class WorktreeError(RuntimeError):
    """A git command or the unchanged check failed."""


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _fail(cmd: list[str], rc: int, stderr: str) -> WorktreeError:
    return WorktreeError(f"{' '.join(cmd)} failed (exit {rc}): {stderr[-_STDERR_TAIL:]}")


def _git(*args: str, cwd: Path | None = None) -> str:
    cmd = ["git", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=cwd, env=_env())
    if proc.returncode != 0:
        raise _fail(cmd, proc.returncode, proc.stderr)
    return proc.stdout


def _git_bytes(*args: str) -> bytes:
    """Like `_git` but keeps stdout as bytes (a diff may hold text that is not UTF-8)."""
    cmd = ["git", *args]
    proc = subprocess.run(cmd, capture_output=True, check=False, env=_env())
    if proc.returncode != 0:
        raise _fail(cmd, proc.returncode, proc.stderr.decode("utf-8", "replace"))
    return proc.stdout


def _pathspec() -> list[str]:
    return ["--", ".", *(f":(exclude){p}" for p in EXCLUDE)]


def _z_names(out: str) -> list[str]:
    return sorted({n for n in out.split("\0") if n})


def prepare_worktree(source: Path, dest: Path) -> str:
    """Make `dest` a detached worktree of `source` holding its current files; return the base.

    The base commit carries the source HEAD, the tracked uncommitted changes and the untracked,
    not ignored files. The source's index, HEAD, working tree and stash list stay as they are.
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
    _git("-C", str(source), "worktree", "add", "--detach", str(dest), rev)

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

    _git("-C", str(dest), "add", "-A")
    _git(
        *_IDENTITY,
        "-C",
        str(dest),
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "harness2 intake base",
    )
    return _git("-C", str(dest), "rev-parse", "HEAD").strip()


def collect_diff(worktree: Path, base_rev: str, out: Path) -> list[str]:
    """Write the worktree's changes since `base_rev` (without `EXCLUDE`) to `out`.

    Returns the changed paths, sorted. When nothing changed, `out` is not written and the
    result is `[]`.
    """
    wt = str(worktree)
    _git("-C", wt, "add", "-A")
    diff = _git_bytes("-C", wt, "diff", "--cached", "--binary", base_rev, *_pathspec())
    if not diff:
        return []
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(diff)
    names = _git("-C", wt, "diff", "--cached", "--name-only", "-z", base_rev, *_pathspec())
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


def _forbidden(path: str) -> bool:
    if path.startswith("/") or "\\" in path or ".." in path.split("/"):
        return True
    return any(path == p or path.startswith(p + "/") for p in EXCLUDE)


def apply_back(source: Path, run_dir: Path, *, unchanged_name: str | None) -> list[str]:
    """Apply the run's diff to the source's working tree; return the changed paths, sorted.

    When `unchanged_name` is given, the worktree's untouched files must pass the check first:
    the source's `docs/pbi/tools/unchanged.py` runs with the worktree as cwd and the source's
    `docs/pbi` as `--dir`, so it compares the worktree's files with the hashes recorded in the
    source. The copies of the script and the record inside the worktree are not used. On any
    failure the source is left as it was.
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

    if unchanged_name is not None:
        script = source / "docs" / "pbi" / "tools" / "unchanged.py"
        if not script.is_file():
            raise WorktreeError(f"no unchanged.py in the source: {script}")
        # `--dir` is an option of the top-level parser, so it goes before the subcommand.
        cmd = [
            sys.executable,
            str(script.resolve()),
            "--dir",
            str((source / "docs" / "pbi").resolve()),
            "check",
            unchanged_name,
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, cwd=worktree, env=_env()
        )
        if proc.returncode != 0:
            changed = [ln for ln in proc.stdout.splitlines() if ln.startswith("changed ")]
            detail = "; ".join(changed) or proc.stdout[-_STDERR_TAIL:]
            raise WorktreeError(
                f"{' '.join(cmd)} failed (exit {proc.returncode}): {detail} "
                f"{proc.stderr[-_STDERR_TAIL:]}".rstrip()
            )

    diff_abs = str(diff.resolve())
    paths = _diff_paths(source, diff.resolve())
    bad = [p for p in paths if _forbidden(p)]
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
