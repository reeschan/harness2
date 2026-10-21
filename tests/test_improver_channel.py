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

"""HARNESS2_IMPROVER_CHANNEL picks the improver channel and the model it takes."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Set to "" (= unset for env()) so a .env next to the package cannot fill them in.
CLEARED = (
    "HARNESS2_SOLVER_MODEL",
    "HARNESS2_CODEX_MODEL",
    "HARNESS2_IMPROVER_MODEL",
    "HARNESS2_IMPROVER_CHANNEL",
    "HARNESS2_JUDGE_MODEL",
    "HARNESS2_JUDGE_BASE_URL",
    "HARNESS2_JUDGE_API_KEY",
    "HARNESS2_CODEX_BASE_URL",
    "HARNESS2_CODEX_CONTAINER_URL",
    "HARNESS2_CODEX_API_KEY",
    "HARNESS2_VERTEX_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
)

CHILD = """
import json
from harness2 import config
from harness2.core.spec import resolve_channel_model

def attempt(fn):
    try:
        return {"ok": fn()}
    except Exception as e:
        return {"error": type(e).__name__, "message": str(e)}

def resolve():
    spec = resolve_channel_model(None, None)
    return [spec.kind, spec.model]

def validate():
    config.validate_models()
    return True

print(json.dumps({
    "channel": config.DEFAULT_PROPOSER_CHANNEL,
    "resolve": attempt(resolve),
    "validate": attempt(validate),
}))
"""


ADC_CHILD = """
import json, sys
from harness2 import preflight
print(json.dumps(preflight._needs_adc(judge=sys.argv[1] == "1", bench=sys.argv[2])))
"""


def run_child(values, code, *args):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("HARNESS2_") and not k.startswith("GOOGLE_CLOUD_")
    }
    env.update(dict.fromkeys(CLEARED, ""))
    env.update(values)
    env["PYTHONPATH"] = str(REPO)
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(
            [sys.executable, "-c", code, *args],
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


def load(values):
    return run_child(values, CHILD)


def needs_adc(values, *, judge, bench):
    return run_child(values, ADC_CHILD, "1" if judge else "0", bench)


class ImproverChannelTests(unittest.TestCase):
    def test_default_is_opencode_with_improver_model(self):
        snap = load({"HARNESS2_SOLVER_MODEL": "prov/model-a"})
        self.assertEqual(snap["channel"], "opencode")
        self.assertEqual(snap["resolve"], {"ok": ["opencode", "prov/model-a"]})
        self.assertEqual(snap["validate"], {"ok": True})

        snap = load(
            {"HARNESS2_SOLVER_MODEL": "prov/model-a", "HARNESS2_IMPROVER_MODEL": "prov/model-b"}
        )
        self.assertEqual(snap["resolve"], {"ok": ["opencode", "prov/model-b"]})
        self.assertEqual(snap["validate"], {"ok": True})

    def test_claude_cli_takes_bare_improver_model(self):
        snap = load(
            {
                "HARNESS2_SOLVER_MODEL": "prov/model-a",
                "HARNESS2_IMPROVER_CHANNEL": "claude_cli",
                "HARNESS2_IMPROVER_MODEL": "model-q",
                "GOOGLE_CLOUD_PROJECT": "proj-x",
            }
        )
        self.assertEqual(snap["channel"], "claude_cli")
        self.assertEqual(snap["resolve"], {"ok": ["claude_cli", "model-q"]})
        self.assertEqual(snap["validate"], {"ok": True})

    def test_claude_cli_needs_vertex_project(self):
        base = {
            "HARNESS2_SOLVER_MODEL": "prov/model-a",
            "HARNESS2_IMPROVER_CHANNEL": "claude_cli",
            "HARNESS2_IMPROVER_MODEL": "model-q",
        }
        snap = load(base)
        self.assertEqual(snap["validate"]["error"], "ConfigError")
        self.assertIn("GOOGLE_CLOUD_PROJECT", snap["validate"]["message"])
        for name in ("GOOGLE_CLOUD_PROJECT", "HARNESS2_VERTEX_PROJECT"):
            with self.subTest(project_env=name):
                snap = load({**base, name: "proj-x"})
                self.assertEqual(snap["validate"], {"ok": True})

    def test_preflight_needs_adc_for_claude_cli(self):
        cases = (
            # (environment, judge, bench, expected)
            ({"HARNESS2_SOLVER_MODEL": "prov/model-a"}, False, "jb", False),
            ({"HARNESS2_SOLVER_MODEL": "prov/model-a"}, False, "lab", False),
            ({"HARNESS2_SOLVER_MODEL": "google-vertex/model-a"}, False, "jb", True),
            (
                {
                    "HARNESS2_SOLVER_MODEL": "prov/model-a",
                    "HARNESS2_IMPROVER_CHANNEL": "claude_cli",
                    "HARNESS2_IMPROVER_MODEL": "model-q",
                },
                False,
                "jb",
                True,
            ),
            ({"HARNESS2_SOLVER_MODEL": "prov/model-a"}, True, "wb", True),
        )
        for values, judge, bench, expected in cases:
            with self.subTest(values=values, judge=judge, bench=bench):
                self.assertIs(needs_adc(values, judge=judge, bench=bench), expected)

    def test_claude_cli_rejects_provider_prefixed_model(self):
        snap = load(
            {
                "HARNESS2_SOLVER_MODEL": "prov/model-a",
                "HARNESS2_IMPROVER_CHANNEL": "claude_cli",
                "HARNESS2_IMPROVER_MODEL": "prov/model-q",
            }
        )
        self.assertEqual(snap["validate"]["error"], "ConfigError")
        self.assertIn("claude_cli", snap["validate"]["message"])
        self.assertEqual(snap["resolve"]["error"], "ValueError")
        self.assertIn("claude_cli", snap["resolve"]["message"])

    def test_claude_cli_needs_improver_model(self):
        snap = load(
            {"HARNESS2_SOLVER_MODEL": "prov/model-a", "HARNESS2_IMPROVER_CHANNEL": "claude_cli"}
        )
        self.assertEqual(snap["validate"]["error"], "ConfigError")
        self.assertIn("HARNESS2_IMPROVER_MODEL", snap["validate"]["message"])
        # No fallback to the provider-prefixed solver model: the default resolves to empty.
        self.assertEqual(snap["resolve"], {"ok": ["claude_cli", ""]})

    def test_unknown_channel_is_a_config_error(self):
        snap = load({"HARNESS2_SOLVER_MODEL": "prov/model-a", "HARNESS2_IMPROVER_CHANNEL": "foo"})
        self.assertEqual(snap["channel"], "foo")
        self.assertEqual(snap["validate"]["error"], "ConfigError")
        self.assertIn("HARNESS2_IMPROVER_CHANNEL", snap["validate"]["message"])
        self.assertIn("foo", snap["validate"]["message"])

    def test_opencode_still_requires_provider_prefix(self):
        for channel in ("", "opencode"):
            with self.subTest(channel=channel):
                snap = load(
                    {
                        "HARNESS2_SOLVER_MODEL": "prov/model-a",
                        "HARNESS2_IMPROVER_CHANNEL": channel,
                        "HARNESS2_IMPROVER_MODEL": "model-q",
                    }
                )
                self.assertEqual(snap["channel"], "opencode")
                self.assertEqual(snap["validate"]["error"], "ConfigError")
                self.assertEqual(snap["resolve"]["error"], "ValueError")
                self.assertIn("opencode", snap["resolve"]["message"])


if __name__ == "__main__":
    unittest.main()
