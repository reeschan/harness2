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

import hashlib
import json
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

# The real checker, copied into each test source repository.
REAL_UNCHANGED = Path(__file__).resolve().parents[1] / "docs" / "pbi" / "tools" / "unchanged.py"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_record(pbi_dir, name, hashes):
    """Write `<pbi_dir>/verify/<name>.unchanged.json` as unchanged.py's save does."""
    rec = pbi_dir / "verify" / f"{name}.unchanged.json"
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(hashes), encoding="utf-8")


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
        # Uncommitted work: a tracked change and untracked files, including docs/pbi/.
        (self.src / "a.txt").write_text("alpha changed\n")
        (self.src / "new.txt").write_text("untracked\n")
        tools = self.src / "docs" / "pbi" / "tools"
        tools.mkdir(parents=True)
        (tools / "x.py").write_text("print('x')\n")
        shutil.copyfile(REAL_UNCHANGED, tools / "unchanged.py")
        # GOOD guards keep.txt (left alone by the agent); BAD guards a.txt (the agent edits it).
        pbi = self.src / "docs" / "pbi"
        write_record(pbi, "GOOD", {"keep.txt": sha(b"keep\n")})
        write_record(pbi, "BAD", {"a.txt": sha(b"alpha changed\n")})
        (self.src / "ignored.log").write_text("noise\n")
        self.run_dir = self.tmp / "run"
        self.wt = self.run_dir / wt.WORKTREE_DIRNAME
        self.diff = self.run_dir / "workspace" / "output" / wt.DIFF_NAME


class PrepareTest(WorktreeTestBase):
    def test_carries_tracked_changes_and_untracked_files(self):
        base = wt.prepare_worktree(self.src, self.wt)
        self.assertEqual(len(base), 40)
        self.assertEqual((self.wt / "a.txt").read_text(), "alpha changed\n")
        self.assertEqual((self.wt / "new.txt").read_text(), "untracked\n")
        self.assertEqual((self.wt / "docs/pbi/tools/x.py").read_text(), "print('x')\n")
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
            wt.prepare_worktree(self.src / "docs", self.wt)
        self.wt.mkdir(parents=True)
        with self.assertRaises(WorktreeError):
            wt.prepare_worktree(self.src, self.wt)


class CollectTest(WorktreeTestBase):
    def setUp(self):
        super().setUp()
        self.base = wt.prepare_worktree(self.src, self.wt)

    def test_no_change_writes_nothing(self):
        self.assertEqual(wt.collect_diff(self.wt, self.base, self.diff), [])
        self.assertFalse(self.diff.exists())

    def test_change_new_delete_and_binary(self):
        (self.wt / "a.txt").write_text("alpha by agent\n")
        (self.wt / "c.txt").write_text("brand new\n")
        (self.wt / "b.txt").unlink()
        (self.wt / "bin.dat").write_bytes(BINARY[::-1])
        (self.wt / "docs/pbi/tools/x.py").write_text("print('edited')\n")
        (self.wt / "docs/pbi/note.md").write_text("scratch\n")
        names = wt.collect_diff(self.wt, self.base, self.diff)
        self.assertEqual(names, ["a.txt", "b.txt", "bin.dat", "c.txt"])
        text = self.diff.read_bytes().decode("utf-8", "replace")
        self.assertIn("GIT binary patch", text)
        self.assertNotIn("docs/pbi", text)

    def test_only_docs_pbi_change_is_empty(self):
        (self.wt / "docs/pbi/tools/x.py").write_text("print('edited')\n")
        self.assertEqual(wt.collect_diff(self.wt, self.base, self.diff), [])
        self.assertFalse(self.diff.exists())


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

    def test_applies_to_source(self):
        head = git(self.src, "rev-parse", "HEAD")
        paths = wt.apply_back(self.src, self.run_dir, unchanged_name="GOOD")
        self.assertEqual(paths, self.names)
        self.assertEqual((self.src / "a.txt").read_text(), "alpha by agent\n")
        self.assertEqual((self.src / "c.txt").read_text(), "brand new\n")
        self.assertFalse((self.src / "b.txt").exists())
        self.assertEqual((self.src / "bin.dat").read_bytes(), BINARY[::-1])
        self.assertEqual(git(self.src, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.src, "diff", "--cached"), "")

    def test_failed_unchanged_check_keeps_source(self):
        before = self.snapshot()
        with self.assertRaises(WorktreeError) as cm:
            wt.apply_back(self.src, self.run_dir, unchanged_name="BAD")
        self.assertIn("changed a.txt", str(cm.exception))
        self.assertEqual(self.snapshot(), before)

    def test_check_uses_source_script_and_record(self):
        # The agent changes an untouched file, then rewrites the worktree's copies of the
        # record and of unchanged.py so that a check inside the worktree would pass.
        (self.wt / "keep.txt").write_text("tampered\n")
        write_record(self.wt / "docs" / "pbi", "GOOD", {"keep.txt": sha(b"tampered\n")})
        (self.wt / "docs/pbi/tools/unchanged.py").write_text("raise SystemExit(0)\n")
        self.diff.unlink()
        self.assertIn("keep.txt", wt.collect_diff(self.wt, self.base, self.diff))
        before = self.snapshot()
        with self.assertRaises(WorktreeError) as cm:
            wt.apply_back(self.src, self.run_dir, unchanged_name="GOOD")
        self.assertIn("changed keep.txt", str(cm.exception))
        self.assertEqual(self.snapshot(), before)

    def test_missing_source_script(self):
        (self.src / "docs/pbi/tools/unchanged.py").unlink()
        before = self.snapshot()
        with self.assertRaises(WorktreeError):
            wt.apply_back(self.src, self.run_dir, unchanged_name="GOOD")
        self.assertEqual(self.snapshot(), before)

    def test_conflict_keeps_source(self):
        (self.src / "a.txt").write_text("someone else\n")
        before = self.snapshot()
        with self.assertRaises(WorktreeError):
            wt.apply_back(self.src, self.run_dir, unchanged_name="GOOD")
        self.assertEqual(self.snapshot(), before)

    def test_rejects_crafted_diff(self):
        crafted = {
            "docs/pbi/evil.txt": "docs/pbi/evil.txt",
            "../escape.txt": "escape.txt",
        }
        for path, landing in crafted.items():
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
                    wt.apply_back(self.src, self.run_dir, unchanged_name=None)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.tmp / landing).exists())

    def test_none_skips_check(self):
        # A check would fail with this script; None must not run it.
        (self.src / "docs/pbi/tools/unchanged.py").write_text("raise SystemExit(1)\n")
        paths = wt.apply_back(self.src, self.run_dir, unchanged_name=None)
        self.assertEqual(paths, self.names)
        self.assertEqual((self.src / "a.txt").read_text(), "alpha by agent\n")

    def test_missing_or_symlinked_diff(self):
        real = self.tmp / "real.diff"
        self.diff.rename(real)
        with self.assertRaises(WorktreeError):
            wt.apply_back(self.src, self.run_dir, unchanged_name=None)
        self.diff.symlink_to(real)
        with self.assertRaises(WorktreeError):
            wt.apply_back(self.src, self.run_dir, unchanged_name=None)


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
