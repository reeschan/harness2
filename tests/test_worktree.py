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

"""core.worktree on real git repositories under a temporary directory."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness2.core import worktree as wt
from harness2.core.worktree import WorktreeError

BINARY = bytes(range(256)) * 4

# Repository location variables that would send git to another repository (set, for example,
# when the tests run from inside a git hook).
GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
)


def git(cwd, *args):
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def state(repo):
    return (
        git(repo, "status", "--porcelain"),
        git(repo, "stash", "list"),
        git(repo, "rev-parse", "HEAD"),
    )


class WorktreeTestBase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
        )
        env.start()
        self.addCleanup(env.stop)
        for name in GIT_LOCATION_VARS:
            os.environ.pop(name, None)  # restored by env.stop
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "src"
        self.src.mkdir()
        git(self.src, "init", "-q", "-b", "main")
        (self.src / "a.txt").write_text("alpha\n")
        (self.src / "b.txt").write_text("beta\n")
        (self.src / "bin.dat").write_bytes(BINARY)
        (self.src / "keep.txt").write_text("keep\n")
        (self.src / ".gitignore").write_text("ignored.log\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "init")
        # Uncommitted work: a tracked change and untracked files, including scratch/.
        (self.src / "a.txt").write_text("alpha changed\n")
        (self.src / "new.txt").write_text("untracked\n")
        (self.src / "scratch").mkdir()
        (self.src / "scratch" / "x.txt").write_text("x\n")
        (self.src / "ignored.log").write_text("noise\n")
        self.run_dir = self.tmp / "run"
        self.wt = self.run_dir / wt.WORKTREE_DIRNAME
        self.diff = self.run_dir / "workspace" / "output" / wt.DIFF_NAME

    def listed(self, repo):
        return git(repo, "worktree", "list", "--porcelain")


class PrepareTest(WorktreeTestBase):
    def test_carries_tracked_changes_and_untracked_files(self):
        base = wt.prepare_worktree(self.src, self.wt)
        self.assertEqual(len(base), 40)
        self.assertEqual((self.wt / "a.txt").read_text(), "alpha changed\n")
        self.assertEqual((self.wt / "new.txt").read_text(), "untracked\n")
        self.assertEqual((self.wt / "scratch/x.txt").read_text(), "x\n")
        self.assertEqual((self.wt / "bin.dat").read_bytes(), BINARY)
        self.assertEqual(git(self.wt, "rev-parse", "HEAD").strip(), base)
        self.assertEqual(git(self.wt, "status", "--porcelain"), "")

    def test_skips_ignored_files(self):
        wt.prepare_worktree(self.src, self.wt)
        self.assertFalse((self.wt / "ignored.log").exists())

    def test_leaves_source_alone(self):
        before = state(self.src)
        wt.prepare_worktree(self.src, self.wt)
        self.assertEqual(state(self.src), before)
        self.assertEqual(git(self.src, "stash", "list"), "")

    def test_rejects_non_root_and_existing_dest(self):
        with self.assertRaises(WorktreeError):
            wt.prepare_worktree(self.src / "scratch", self.wt)
        self.wt.mkdir(parents=True)
        with self.assertRaises(WorktreeError):
            wt.prepare_worktree(self.src, self.wt)


class HooksTest(WorktreeTestBase):
    def test_source_hooks_do_not_run(self):
        hooks = self.src / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        marks = {}
        for name in ("pre-commit", "post-checkout"):
            marks[name] = self.tmp / f"ran-{name}"
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\necho ran > '{marks[name]}'\nexit 1\n")
            hook.chmod(0o755)

        base = wt.prepare_worktree(self.src, self.wt)
        (self.wt / "a.txt").write_text("alpha by agent\n")
        self.assertEqual(wt.collect_diff(self.wt, base, self.diff), ["a.txt"])
        for name, mark in marks.items():
            self.assertFalse(mark.exists(), name)

        # Control: the hooks are live for a plain commit in the source.
        proc = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty"]
            + ["-q", "-m", "x"],
            cwd=self.src,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(marks["pre-commit"].exists())


class CleanupTest(WorktreeTestBase):
    def failing_git(self, *fail_on):
        real = wt._git

        def run(*args, **kwargs):
            for word, message in fail_on:
                if word in args:
                    raise WorktreeError(message)
            return real(*args, **kwargs)

        return run

    def assert_gone(self):
        self.assertFalse(self.wt.exists())
        self.assertNotIn(str(self.run_dir.resolve()), self.listed(self.src))
        self.assertNotIn(str(self.run_dir), self.listed(self.src))

    def test_failure_after_add_removes_worktree(self):
        fake = self.failing_git(("commit", "commit boom"))
        with mock.patch.object(wt, "_git", side_effect=fake):
            with self.assertRaises(WorktreeError) as cm:
                wt.prepare_worktree(self.src, self.wt)
        self.assertIn("commit boom", str(cm.exception))
        self.assert_gone()
        # The path is free again.
        wt.prepare_worktree(self.src, self.wt)
        self.assertTrue((self.wt / "new.txt").is_file())

    def test_failed_cleanup_keeps_original_error(self):
        fake = self.failing_git(
            ("commit", "commit boom"), ("remove", "remove boom"), ("prune", "prune boom")
        )
        with mock.patch.object(wt, "_git", side_effect=fake):
            with self.assertRaises(WorktreeError) as cm:
                wt.prepare_worktree(self.src, self.wt)
        self.assertIn("commit boom", str(cm.exception))
        self.assertFalse(self.wt.exists())


class GitEnvTest(WorktreeTestBase):
    def test_drops_location_vars(self):
        with mock.patch.dict(os.environ, {n: "/nowhere" for n in GIT_LOCATION_VARS}):
            env = wt.git_env()
        for name in GIT_LOCATION_VARS:
            self.assertNotIn(name, env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_works_on_the_named_repository(self):
        other = self.tmp / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        (other / "o.txt").write_text("other\n")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "other")
        foreign = {
            "GIT_DIR": str(other / ".git"),
            "GIT_WORK_TREE": str(other),
            "GIT_INDEX_FILE": str(other / ".git" / "index"),
        }
        with mock.patch.dict(os.environ, foreign):
            base = wt.prepare_worktree(self.src, self.wt)
            (self.wt / "a.txt").write_text("alpha by agent\n")
            names = wt.collect_diff(self.wt, base, self.diff)
            paths = wt.apply_back(self.src, self.run_dir)
        self.assertEqual(names, ["a.txt"])
        self.assertEqual(paths, ["a.txt"])
        self.assertEqual((self.src / "a.txt").read_text(), "alpha by agent\n")
        self.assertFalse((self.wt / "o.txt").exists())
        self.assertIn(str(self.run_dir.resolve()), self.listed(self.src))
        self.assertNotIn(str(self.run_dir.resolve()), self.listed(other))
        self.assertEqual(git(other, "status", "--porcelain"), "")


class CollectTest(WorktreeTestBase):
    def setUp(self):
        super().setUp()
        self.base = wt.prepare_worktree(self.src, self.wt)

    def stale_diff(self):
        """Leave an old diff where an earlier run would have written one."""
        self.diff.parent.mkdir(parents=True, exist_ok=True)
        self.diff.write_bytes(b"stale diff\n")

    def symlink_diff(self):
        """Point the output path at another file; return that file."""
        target = self.tmp / "elsewhere.diff"
        target.write_bytes(b"elsewhere\n")
        self.diff.parent.mkdir(parents=True, exist_ok=True)
        self.diff.symlink_to(target)
        return target

    def test_no_change_writes_nothing(self):
        self.assertEqual(wt.collect_diff(self.wt, self.base, self.diff), [])
        self.assertFalse(self.diff.exists())

    def test_no_change_removes_a_stale_diff(self):
        self.stale_diff()
        self.assertEqual(wt.collect_diff(self.wt, self.base, self.diff), [])
        self.assertFalse(self.diff.exists())

    def test_no_change_removes_a_stale_diff_symlink(self):
        target = self.symlink_diff()
        self.assertEqual(wt.collect_diff(self.wt, self.base, self.diff), [])
        self.assertFalse(self.diff.is_symlink())
        self.assertFalse(self.diff.exists())
        self.assertEqual(target.read_bytes(), b"elsewhere\n")

    def test_change_replaces_a_diff_symlink(self):
        target = self.symlink_diff()
        (self.wt / "a.txt").write_text("alpha by agent\n")
        names = wt.collect_diff(self.wt, self.base, self.diff)
        self.assertEqual(names, ["a.txt"])
        self.assertFalse(self.diff.is_symlink())
        self.assertIn("alpha by agent", self.diff.read_bytes().decode("utf-8", "replace"))
        self.assertEqual(target.read_bytes(), b"elsewhere\n")

    def test_only_excluded_change_removes_a_stale_diff(self):
        self.stale_diff()
        (self.wt / "scratch/x.txt").write_text("edited\n")
        self.assertEqual(wt.collect_diff(self.wt, self.base, self.diff, exclude=("scratch",)), [])
        self.assertFalse(self.diff.exists())

    def test_change_new_delete_and_binary(self):
        (self.wt / "a.txt").write_text("alpha by agent\n")
        (self.wt / "c.txt").write_text("brand new\n")
        (self.wt / "b.txt").unlink()
        (self.wt / "bin.dat").write_bytes(BINARY[::-1])
        names = wt.collect_diff(self.wt, self.base, self.diff)
        self.assertEqual(names, ["a.txt", "b.txt", "bin.dat", "c.txt"])
        self.assertIn("GIT binary patch", self.diff.read_bytes().decode("utf-8", "replace"))

    def test_exclude_keeps_paths_out(self):
        (self.wt / "a.txt").write_text("alpha by agent\n")
        (self.wt / "scratch/x.txt").write_text("edited\n")
        (self.wt / "scratch/note.md").write_text("note\n")
        names = wt.collect_diff(self.wt, self.base, self.diff, exclude=("scratch",))
        self.assertEqual(names, ["a.txt"])
        self.assertNotIn("scratch", self.diff.read_bytes().decode("utf-8", "replace"))

    def test_only_excluded_change_is_empty(self):
        (self.wt / "scratch/x.txt").write_text("edited\n")
        self.assertEqual(wt.collect_diff(self.wt, self.base, self.diff, exclude=("scratch",)), [])
        self.assertFalse(self.diff.exists())

    def test_default_excludes_nothing(self):
        (self.wt / "a.txt").write_text("alpha by agent\n")
        (self.wt / "scratch/x.txt").write_text("edited\n")
        names = wt.collect_diff(self.wt, self.base, self.diff)
        self.assertEqual(names, ["a.txt", "scratch/x.txt"])


class ApplyBackTest(WorktreeTestBase):
    def setUp(self):
        super().setUp()
        self.base = wt.prepare_worktree(self.src, self.wt)
        (self.wt / "a.txt").write_text("alpha by agent\n")
        (self.wt / "c.txt").write_text("brand new\n")
        (self.wt / "b.txt").unlink()
        (self.wt / "bin.dat").write_bytes(BINARY[::-1])
        self.names = wt.collect_diff(self.wt, self.base, self.diff)

    def snapshot(self):
        files = {}
        for p in sorted(self.src.rglob("*")):
            if ".git" in p.relative_to(self.src).parts or not p.is_file():
                continue
            files[p.relative_to(self.src).as_posix()] = p.read_bytes()
        return files, git(self.src, "rev-parse", "HEAD"), git(self.src, "diff", "--cached")

    def assert_applied(self, paths):
        self.assertEqual(paths, self.names)
        self.assertEqual((self.src / "a.txt").read_text(), "alpha by agent\n")
        self.assertEqual((self.src / "c.txt").read_text(), "brand new\n")
        self.assertFalse((self.src / "b.txt").exists())
        self.assertEqual((self.src / "bin.dat").read_bytes(), BINARY[::-1])

    def test_applies_to_source(self):
        head = git(self.src, "rev-parse", "HEAD")
        self.assert_applied(wt.apply_back(self.src, self.run_dir))
        self.assertEqual(git(self.src, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.src, "diff", "--cached"), "")

    def test_guard_none_applies(self):
        self.assert_applied(wt.apply_back(self.src, self.run_dir, guard=lambda w, s: None))

    def test_guard_gets_worktree_and_source(self):
        calls = []

        def guard(worktree, source):
            calls.append((worktree, source))
            # Nothing is applied yet when the guard runs.
            self.assertEqual((source / "a.txt").read_text(), "alpha changed\n")
            return None

        wt.apply_back(self.src, self.run_dir, guard=guard)
        self.assertEqual(calls, [(self.wt, self.src)])

    def test_guard_reason_keeps_source(self):
        before = self.snapshot()
        with self.assertRaises(WorktreeError) as cm:
            wt.apply_back(self.src, self.run_dir, guard=lambda w, s: "keep.txt was touched")
        self.assertEqual(str(cm.exception), "keep.txt was touched")
        self.assertEqual(self.snapshot(), before)

    def test_guard_exception_goes_up_and_keeps_source(self):
        def guard(worktree, source):
            raise ValueError("guard broke")

        before = self.snapshot()
        with self.assertRaises(ValueError):
            wt.apply_back(self.src, self.run_dir, guard=guard)
        self.assertEqual(self.snapshot(), before)

    def test_conflict_keeps_source(self):
        (self.src / "a.txt").write_text("someone else\n")
        before = self.snapshot()
        with self.assertRaises(WorktreeError):
            wt.apply_back(self.src, self.run_dir)
        self.assertEqual(self.snapshot(), before)

    def test_rejects_crafted_diff(self):
        crafted = {
            "../escape.txt": (self.tmp / "escape.txt", ()),
            "scratch/evil.txt": (self.src / "scratch/evil.txt", ("scratch",)),
        }
        for path, (landing, exclude) in crafted.items():
            with self.subTest(path=path):
                self.diff.write_text(
                    f"diff --git a/{path} b/{path}\n"
                    "new file mode 100644\n"
                    "--- /dev/null\n"
                    f"+++ b/{path}\n"
                    "@@ -0,0 +1 @@\n"
                    "+evil\n"
                )
                before = self.snapshot()
                with self.assertRaises(WorktreeError):
                    wt.apply_back(self.src, self.run_dir, exclude=exclude)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(landing.exists())

    def test_missing_or_symlinked_diff(self):
        real = self.tmp / "real.diff"
        self.diff.rename(real)
        with self.assertRaises(WorktreeError):
            wt.apply_back(self.src, self.run_dir)
        self.diff.symlink_to(real)
        with self.assertRaises(WorktreeError):
            wt.apply_back(self.src, self.run_dir)


class RemoveTest(WorktreeTestBase):
    def test_removes_and_unregisters(self):
        wt.prepare_worktree(self.src, self.wt)
        self.assertIn(str(self.wt.resolve()), git(self.src, "worktree", "list"))
        wt.remove_worktree(self.src, self.wt)
        self.assertFalse(self.wt.exists())
        self.assertNotIn(str(self.wt.resolve()), git(self.src, "worktree", "list"))
        wt.remove_worktree(self.src, self.wt)  # gone already: nothing happens


if __name__ == "__main__":
    unittest.main()
