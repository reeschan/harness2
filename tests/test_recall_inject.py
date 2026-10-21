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

"""Recall block injection, sizing and removal in core.recall."""

import copy
import dataclasses
import unittest

from harness2.core import context, recall
from harness2.core import tree as harness_tree
from harness2.substrate.codex import CODEX_SURFACE
from harness2.substrate.opencode import OPENCODE_SURFACE

OC = OPENCODE_SURFACE
CX = CODEX_SURFACE


def oc_tree() -> harness_tree.Tree:
    return {
        "systemprompt.md": "You are an agent.\n",
        "guardrails.md": "- Be careful.\n",
        "LongTermMEMORY.md": "# Memory\nold note\n",
        "tool_descriptions/tool_guidance.md": "Use tools.\n",
        "skills/x/SKILL.md": "---\nname: x\ndescription: d\n---\n",
    }


class TargetFileTest(unittest.TestCase):
    def test_opencode_uses_memory_file(self):
        self.assertEqual(recall.target_file(OC), "LongTermMEMORY.md")

    def test_codex_uses_first_always_on(self):
        self.assertEqual(recall.target_file(CX), "AGENTS.md")

    def test_memory_file_overrides_first_always_on(self):
        s = dataclasses.replace(CX, memory_file="OTHER.md")
        self.assertEqual(recall.target_file(s), "OTHER.md")


class InjectTest(unittest.TestCase):
    def test_appends_block_to_target_only(self):
        t = oc_tree()
        before = copy.deepcopy(t)
        out = recall.inject(OC, t, "remember this")
        self.assertEqual(t, before)
        self.assertIsNot(out, t)
        body = out["LongTermMEMORY.md"]
        self.assertTrue(body.startswith(t["LongTermMEMORY.md"]))
        self.assertTrue(
            body.endswith(recall.BEGIN + "\n" + "remember this" + "\n" + recall.END + "\n")
        )
        self.assertEqual(
            body,
            t["LongTermMEMORY.md"]
            + "\n\n"
            + recall.BEGIN
            + "\nremember this\n"
            + recall.END
            + "\n",
        )
        for rel in t:
            if rel != "LongTermMEMORY.md":
                self.assertEqual(out[rel], t[rel])
        self.assertEqual(set(out), set(t))

    def test_codex_appends_to_agents_md(self):
        t = {"AGENTS.md": "# Agents\n"}
        out = recall.inject(CX, t, "x")
        self.assertTrue(out["AGENTS.md"].startswith("# Agents\n"))
        self.assertIn(recall.BEGIN + "\nx\n" + recall.END, out["AGENTS.md"])

    def test_empty_text_returns_same_contents(self):
        t = oc_tree()
        out = recall.inject(OC, t, "")
        self.assertEqual(out, t)
        self.assertIsNot(out, t)

    def test_grader_verdict_marker_raises(self):
        t = oc_tree()
        before = copy.deepcopy(t)
        for m in context.GRADER_VERDICT_MARKERS:
            with self.subTest(marker=m):
                with self.assertRaises(ValueError):
                    recall.inject(OC, t, f"note {m} here")
        self.assertEqual(t, before)

    def test_reserved_delimiter_raises(self):
        t = oc_tree()
        before = copy.deepcopy(t)
        for m in (recall.BEGIN, recall.END):
            with self.subTest(marker=m):
                with self.assertRaises(ValueError) as cm:
                    recall.inject(OC, t, "lesson " + m + " tail")
                self.assertIn("reserved recall delimiter", str(cm.exception))
                self.assertEqual(t, before)


class RoomTest(unittest.TestCase):
    def test_text_of_room_size_fits_cap(self):
        s = dataclasses.replace(OC, always_on_max=500)
        t = oc_tree()
        n = recall.room(s, t)
        self.assertGreater(n, 0)
        out = recall.inject(s, t, "a" * n)
        self.assertLessEqual(len(harness_tree.always_on_text(s, out)), s.always_on_max)
        self.assertEqual(len(harness_tree.always_on_text(s, out)), s.always_on_max)

    def test_default_cap_fits(self):
        t = oc_tree()
        n = recall.room(OC, t)
        out = recall.inject(OC, t, "b" * n)
        self.assertLessEqual(len(harness_tree.always_on_text(OC, out)), OC.always_on_max)

    def test_codex_fits(self):
        t = {"AGENTS.md": "# Agents\n" + "z" * 100}
        n = recall.room(CX, t)
        out = recall.inject(CX, t, "c" * n)
        self.assertLessEqual(len(harness_tree.always_on_text(CX, out)), CX.always_on_max)

    def test_near_cap_has_no_room(self):
        t = oc_tree()
        used = len(harness_tree.always_on_text(OC, t))
        t["systemprompt.md"] += "y" * (OC.always_on_max - used - 10)
        self.assertLessEqual(
            OC.always_on_max - len(harness_tree.always_on_text(OC, t)), len(recall.BEGIN)
        )
        self.assertLessEqual(recall.room(OC, t), 0)

    def test_full_cap_has_no_room(self):
        t = {"AGENTS.md": "q" * CX.always_on_max}
        self.assertLessEqual(recall.room(CX, t), 0)


class StripTest(unittest.TestCase):
    def test_strip_inverts_inject(self):
        cases = [
            (OC, oc_tree()),
            (OC, {**oc_tree(), "LongTermMEMORY.md": ""}),
            (OC, {**oc_tree(), "LongTermMEMORY.md": "no trailing newline"}),
            (OC, {**oc_tree(), "LongTermMEMORY.md": "many newlines\n\n\n"}),
            (CX, {"AGENTS.md": "# Agents\n"}),
            (CX, {".agents/skills/s/SKILL.md": "---\ndescription: d\n---\n"}),
        ]
        for surface, t in cases:
            for text in ("one line", "two\nlines\n", "x"):
                with self.subTest(surface=surface.name, tree=t, text=text):
                    before = copy.deepcopy(t)
                    injected = recall.inject(surface, t, text)
                    self.assertNotEqual(injected, t)
                    self.assertEqual(recall.strip(injected), t)
                    self.assertEqual(t, before)

    def test_strip_without_block_changes_nothing(self):
        t = oc_tree()
        before = copy.deepcopy(t)
        out = recall.strip(t)
        self.assertEqual(out, t)
        self.assertIsNot(out, t)
        self.assertEqual(t, before)

    def test_strip_removes_block_from_any_file(self):
        t = oc_tree()
        injected = recall.inject(OC, t, "r")
        moved = dict(t)
        moved["guardrails.md"] = injected["LongTermMEMORY.md"]
        out = recall.strip(moved)
        self.assertNotIn(recall.BEGIN, out["guardrails.md"])
        self.assertNotIn(recall.END, out["guardrails.md"])
        self.assertEqual(out["guardrails.md"], t["LongTermMEMORY.md"])


if __name__ == "__main__":
    unittest.main()
