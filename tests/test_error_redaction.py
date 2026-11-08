"""Error messages quote a hash and a length, never the body that produced them."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness2.core import context, proposer, trace
from harness2.core.spec import ChannelSpec

MARKER = "deterministic_eval_summary"


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class TreeVerdictMessageTest(unittest.TestCase):
    def test_body_match_reports_hash_not_excerpt(self) -> None:
        before = "PREFIX-MARK-ONE "
        after = " SUFFIX-MARK-TWO"
        body = before + MARKER + after
        where = body.index(MARKER)
        with self.assertRaises(ValueError) as caught:
            context.assert_tree_has_no_grader_verdicts({"harness.md": body})
        message = str(caught.exception)
        self.assertNotIn(before.strip(), message)
        self.assertNotIn(after.strip(), message)
        excerpt = body[max(0, where - 60) : where + 120]
        self.assertIn(f"excerpt sha256={trace.digest(excerpt)}", message)
        self.assertIn(f"chars={len(excerpt)}", message)
        self.assertIn("harness.md", message)
        self.assertIn(MARKER, message)
        self.assertIn(f"at offset {where}", message)

    def test_disk_body_match_reports_hash_not_excerpt(self) -> None:
        before = "PREFIX-DISK-ONE "
        after = " SUFFIX-DISK-TWO"
        body = before + MARKER + after
        where = body.index(MARKER)
        excerpt = body[max(0, where - 60) : where + 120]
        with tempfile.TemporaryDirectory() as d:
            Path(d, "notes.md").write_text(body, encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                context.assert_no_grader_verdicts(Path(d))
        message = str(caught.exception)
        self.assertNotIn(before.strip(), message)
        self.assertNotIn(after.strip(), message)
        self.assertIn(f"excerpt sha256={trace.digest(excerpt)}", message)
        self.assertIn(f"chars={len(excerpt)}", message)
        self.assertIn("notes.md", message)
        self.assertIn(MARKER, message)
        self.assertIn(f"at offset {where}", message)

    def test_name_match_message_is_unchanged(self) -> None:
        rel = f"{MARKER}.md"
        with self.assertRaises(ValueError) as caught:
            context.assert_tree_has_no_grader_verdicts({rel: "clean body"})
        self.assertEqual(
            str(caught.exception),
            f"harness file {rel!r} has grader-verdict marker {MARKER!r} in its NAME; rename "
            f"it -- files carrying these strings cannot be served as evidence to later "
            f"tasks",
        )


class ClaudeCliErrorMessageTest(unittest.TestCase):
    def test_error_reply_is_not_quoted(self) -> None:
        reply = "SECRET-REPLY-BODY that must not appear in the error"
        payload = {
            "result": reply,
            "is_error": True,
            "subtype": "error_max_turns",
            "total_cost_usd": 0.0,
        }
        p = proposer.Proposer(
            spec=ChannelSpec(kind="claude_cli", model="model-q"), project="proj-x", region="r-1"
        )
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(
                proposer.subprocess, "run", return_value=_completed(json.dumps(payload))
            ):
                text, _cost, err = p._claude_cli(Path(d), "mission", "", 30)
        self.assertEqual(text, "")
        self.assertNotIn(reply, err)
        self.assertIn("reply sha256=", err)
        self.assertIn(f"sha256={trace.digest(reply)}", err)
        self.assertIn(f"chars={len(reply)}", err)
        self.assertIn("error_max_turns", err)


if __name__ == "__main__":
    unittest.main()
