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

"""The local folder task connector of core.intake_local."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness2.core import intake
from harness2.core.intake_local import LocalFileConnector


class LocalFileConnectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "root"
        self.inbox = self.root / "inbox"
        self.inbox.mkdir(parents=True)
        self.conn = LocalFileConnector(self.root)

    def _task(self, task_id: str, **files: str) -> Path:
        folder = self.inbox / task_id
        folder.mkdir(parents=True)
        for name, text in files.items():
            path = folder / name.replace("__", "/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return folder

    def _ids(self) -> list[str]:
        return [raw.id for raw in self.conn.poll()]

    def test_root_is_required(self) -> None:
        with self.assertRaises(TypeError):
            LocalFileConnector()  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            LocalFileConnector(None)  # type: ignore[arg-type]

    def test_extra_options_are_ignored(self) -> None:
        conn = LocalFileConnector(root=self.root, unused=1)
        self.assertEqual(conn.root, self.root)

    def test_task_json(self) -> None:
        meta = {"task_spec": {"task_id": "a1"}}
        self._task(
            "a1",
            **{"task.json": json.dumps({"title": "T", "body": "B", "meta": meta})},
        )
        (raw,) = self.conn.poll()
        self.assertEqual((raw.id, raw.title, raw.body), ("a1", "T", "B"))
        self.assertEqual(dict(raw.meta), meta)
        self.assertEqual(raw.files, ())

    def test_task_md_heading_is_the_title(self) -> None:
        self._task("m1", **{"task.md": "intro\n# Heading\n\ntext\n"})
        (raw,) = self.conn.poll()
        self.assertEqual(raw.title, "Heading")
        self.assertEqual(raw.body, "intro\n# Heading\n\ntext\n")
        self.assertEqual(dict(raw.meta), {})

    def test_task_md_without_heading_uses_the_id(self) -> None:
        self._task("m2", **{"task.md": "just text\n"})
        (raw,) = self.conn.poll()
        self.assertEqual(raw.title, "m2")
        self.assertEqual(raw.body, "just text\n")

    def test_task_json_wins_over_task_md(self) -> None:
        self._task(
            "b1",
            **{
                "task.json": json.dumps({"title": "from json", "body": "J"}),
                "task.md": "# from md\nM\n",
            },
        )
        (raw,) = self.conn.poll()
        self.assertEqual((raw.title, raw.body), ("from json", "J"))

    def test_attachments_are_listed_in_name_order(self) -> None:
        self._task(
            "f1",
            **{
                "task.md": "# f\n",
                "files__b.txt": "b",
                "files__a.txt": "a",
                "files__sub__c.txt": "c",
            },
        )
        (raw,) = self.conn.poll()
        self.assertEqual(raw.files, ("a.txt", "b.txt", "sub/c.txt"))

    def test_folders_in_name_order_and_bad_ids_skipped(self) -> None:
        for task_id in ("b", "a", "c.1", "-bad", ".hidden", "x" * 129):
            self._task(task_id, **{"task.md": "# t\n"})
        (self.inbox / "loose.txt").write_text("not a folder", encoding="utf-8")
        self.assertEqual(self._ids(), ["a", "b", "c.1"])

    def test_no_inbox_polls_nothing(self) -> None:
        conn = LocalFileConnector(self.base / "empty")
        self.assertEqual(list(conn.poll()), [])

    def test_delivered_task_is_not_polled(self) -> None:
        self._task("d1", **{"task.md": "# d\n"})
        self._task("d2", **{"task.md": "# d\n"})
        out = self.base / "deliverable"
        (out / "sub").mkdir(parents=True)
        (out / "report.md").write_text("done", encoding="utf-8")
        (out / "sub" / "x.txt").write_text("x", encoding="utf-8")
        self.conn.deliver("d1", out, {"task_id": "d1", "score": 1, "note": "é"})
        self.assertEqual(self._ids(), ["d2"])
        box = self.root / "outbox" / "d1"
        self.assertEqual((box / "report.md").read_text(encoding="utf-8"), "done")
        self.assertEqual((box / "sub" / "x.txt").read_text(encoding="utf-8"), "x")
        text = (box / "summary.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(text), {"task_id": "d1", "score": 1, "note": "é"})
        self.assertEqual(
            text.rstrip("\n"),
            json.dumps(
                {"task_id": "d1", "score": 1, "note": "é"},
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            ),
        )

    def test_deliver_replaces_the_outbox_folder(self) -> None:
        first = self.base / "first"
        first.mkdir()
        (first / "old.txt").write_text("old", encoding="utf-8")
        self.conn.deliver("r1", first, {})
        second = self.base / "second"
        second.mkdir()
        (second / "new.txt").write_text("new", encoding="utf-8")
        self.conn.deliver("r1", second, {"n": 2})
        box = self.root / "outbox" / "r1"
        self.assertEqual(sorted(p.name for p in box.iterdir()), ["new.txt", "summary.json"])

    def test_deliver_without_deliverable_dir_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.conn.deliver("d1", self.base / "missing", {})
        self.assertFalse((self.root / "outbox" / "d1").exists())

    def test_failed_task_is_not_polled(self) -> None:
        self._task("e1", **{"task.md": "# e\n"})
        self._task("e2", **{"task.md": "# e\n"})
        self.conn.fail("e1", "could not normalize")
        self.assertEqual(self._ids(), ["e2"])
        record = json.loads((self.root / "failed" / "e1.json").read_text(encoding="utf-8"))
        self.assertEqual(record, {"task_id": "e1", "reason": "could not normalize"})

    def test_bad_task_ids_raise_and_write_nothing_outside(self) -> None:
        out = self.base / "deliverable"
        out.mkdir()
        for bad in ("../x", "../../escape", "a/b", "", ".x", "x\n"):
            with self.subTest(task_id=bad):
                with self.assertRaises(ValueError):
                    self.conn.deliver(bad, out, {})
                with self.assertRaises(ValueError):
                    self.conn.fail(bad, "r")
        self.assertFalse((self.root / "x").exists())
        self.assertFalse((self.root / "x.json").exists())
        self.assertFalse((self.base / "escape").exists())
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["deliverable", "root"])

    def test_fetch_files_copies_the_attachments(self) -> None:
        self._task("g1", **{"task.md": "# g\n", "files__a.txt": "a", "files__s__b.txt": "b"})
        (raw,) = self.conn.poll()
        dest = self.base / "dest"
        self.conn.fetch_files(raw, dest)
        self.assertEqual((dest / "a.txt").read_text(encoding="utf-8"), "a")
        self.assertEqual((dest / "s" / "b.txt").read_text(encoding="utf-8"), "b")

    def test_fetch_files_without_files_does_nothing(self) -> None:
        self._task("g2", **{"task.md": "# g\n"})
        (raw,) = self.conn.poll()
        dest = self.base / "dest"
        self.conn.fetch_files(raw, dest)
        self.assertFalse(dest.exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symbolic links")
    def test_fetch_files_skips_symbolic_links(self) -> None:
        folder = self._task("h1", **{"task.md": "# h\n", "files__real.txt": "r"})
        secret_dir = self.base / "secret"
        secret_dir.mkdir()
        (secret_dir / "key.txt").write_text("secret", encoding="utf-8")
        try:
            os.symlink(secret_dir / "key.txt", folder / "files" / "link.txt")
            os.symlink(secret_dir, folder / "files" / "linkdir", target_is_directory=True)
        except OSError as exc:  # pragma: no cover - platforms without symlink rights
            self.skipTest(f"cannot create symbolic links: {exc}")
        (raw,) = self.conn.poll()
        self.assertEqual(raw.files, ("real.txt",))
        dest = self.base / "dest"
        self.conn.fetch_files(raw, dest)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["real.txt"])

    def test_broken_task_json_is_failed_and_others_are_polled(self) -> None:
        self._task("a", **{"task.json": json.dumps({"title": "ok", "body": "x"})})
        self._task("b", **{"task.json": "not json"})
        self.assertEqual([(r.id, r.title) for r in self.conn.poll()], [("a", "ok")])
        record = json.loads((self.root / "failed" / "b.json").read_text(encoding="utf-8"))
        self.assertEqual(record["task_id"], "b")
        self.assertIn("task.json", record["reason"])
        self.assertIn("JSON", record["reason"])
        self.assertEqual(self._ids(), ["a"])

    def test_each_kind_of_broken_task_json_is_failed(self) -> None:
        cases = {
            "list": ("[1, 2]", "object"),
            "title": (json.dumps({"title": 1}), "'title'"),
            "body": (json.dumps({"body": ["x"]}), "'body'"),
            "meta": (json.dumps({"meta": [1]}), "'meta'"),
        }
        for task_id, (text, _) in cases.items():
            self._task(task_id, **{"task.json": text})
        self._task("good", **{"task.md": "# g\n"})
        self.assertEqual(self._ids(), ["good"])
        for task_id, (_, word) in cases.items():
            with self.subTest(task_id=task_id):
                path = self.root / "failed" / f"{task_id}.json"
                reason = json.loads(path.read_text(encoding="utf-8"))["reason"]
                self.assertIn("task.json", reason)
                self.assertIn(word, reason)

    def _failed_reason(self, task_id: str) -> str:
        path = self.root / "failed" / f"{task_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))["reason"]

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symbolic links")
    def test_linked_task_files_are_failed_and_not_read(self) -> None:
        secret = self.base / "secret.txt"
        secret.write_text('{"title": "leak", "body": "SECRET"}', encoding="utf-8")
        self._task("a", **{"task.json": json.dumps({"title": "ok", "body": "x"})})
        linked_json = self._task("b", **{"task.md": "# fallback\n"})
        linked_md = self._task("c")
        try:
            os.symlink(secret, linked_json / "task.json")
            os.symlink(secret, linked_md / "task.md")
        except OSError as exc:  # pragma: no cover - platforms without symlink rights
            self.skipTest(f"cannot create symbolic links: {exc}")
        tasks = list(self.conn.poll())
        self.assertEqual([(r.id, r.title) for r in tasks], [("a", "ok")])
        self.assertNotIn("SECRET", "".join(r.body for r in tasks))
        for task_id, name in (("b", "task.json"), ("c", "task.md")):
            with self.subTest(task_id=task_id):
                reason = self._failed_reason(task_id)
                self.assertIn(name, reason)
                self.assertIn("symbolic link", reason)
                self.assertNotIn("SECRET", reason)
        self.assertEqual(self._ids(), ["a"])

    def test_unreadable_task_file_is_failed_and_others_are_polled(self) -> None:
        self._task("a", **{"task.json": json.dumps({"title": "ok", "body": "x"})})
        (self._task("b", **{"task.md": "# fallback\n"}) / "task.json").mkdir()
        (self._task("c") / "task.md").mkdir()
        self.assertEqual([(r.id, r.title) for r in self.conn.poll()], [("a", "ok")])
        for task_id, name in (("b", "task.json"), ("c", "task.md")):
            with self.subTest(task_id=task_id):
                self.assertIn(name, self._failed_reason(task_id))
        self.assertEqual(self._ids(), ["a"])

    def test_task_md_that_is_not_utf8_is_failed(self) -> None:
        folder = self._task("u")
        (folder / "task.md").write_bytes(b"# t\n\xff\xfe\n")
        self._task("v", **{"task.md": "# v\n"})
        self.assertEqual(self._ids(), ["v"])
        reason = self._failed_reason("u")
        self.assertIn("task.md", reason)
        self.assertIn("UTF-8", reason)

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symbolic links")
    def test_deliver_skips_symbolic_links(self) -> None:
        outside = self.base / "private.json"
        outside.write_text("secret", encoding="utf-8")
        out = self.base / "deliverable"
        out.mkdir()
        (out / "report.md").write_text("done", encoding="utf-8")
        try:
            os.symlink(outside, out / "leak")
            os.symlink(self.base, out / "leakdir", target_is_directory=True)
        except OSError as exc:  # pragma: no cover - platforms without symlink rights
            self.skipTest(f"cannot create symbolic links: {exc}")
        self.conn.deliver("s1", out, {})
        box = self.root / "outbox" / "s1"
        self.assertEqual(sorted(p.name for p in box.iterdir()), ["report.md", "summary.json"])
        self.assertFalse((box / "leak").is_symlink())

    def test_connector_for_local(self) -> None:
        conn = intake.connector_for(name="local", root=self.root)
        self.assertIsInstance(conn, LocalFileConnector)
        self.assertIsInstance(conn, intake.TaskConnector)
        self.assertEqual(conn.root, self.root)

    def test_local_is_the_default(self) -> None:
        conn = intake.connector_for(env={}, root=self.root)
        self.assertIsInstance(conn, LocalFileConnector)


if __name__ == "__main__":
    unittest.main()
